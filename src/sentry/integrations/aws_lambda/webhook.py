from __future__ import absolute_import

import logging
import json
import uuid

from base64 import b64decode
from django.views.decorators.csrf import csrf_exempt

from requests.exceptions import RequestException

from sentry import http
from sentry.api.base import Endpoint
from sentry.models import Project, ProjectKey
from sentry.utils.http import absolute_uri
from sentry.utils.strings import gunzip
from sentry.web.decorators import transaction_start

logger = logging.getLogger("sentry.integrations.aws_lambda.webhooks")

# Example error:
# {
#   "errorType": "TypeError",
#   "errorMessage": "Cannot read property 'lol' of undefined",
#   "trace": [
#     "TypeError: Cannot read property 'lol' of undefined",
#     "    at Runtime.exports.handler (/var/task/index.js:3:15)",
#     "    at Runtime.handleOnce (/var/runtime/Runtime.js:66:25)"
#   ]
# }

# Python
# {
#   "errorMessage": "'dict' object has no attribute 'lol'",
#   "errorType": "AttributeError",
#   "stackTrace": [
#     [
#       "/var/task/lambda_function.py",
#       5,
#       "lambda_handler",
#       "event.lol.lol"
#     ]
#   ]
# }

# hard coded to the first org you have (should work in a dev environment)
ORG_ID = 1


class AwsLambdaWebhookEndpoint(Endpoint):
    authentication_classes = ()
    permission_classes = ()

    @csrf_exempt
    def dispatch(self, request, *args, **kwargs):
        return super(AwsLambdaWebhookEndpoint, self).dispatch(request, *args, **kwargs)

    @transaction_start("AwsLambdaWebhookEndpoint")
    def post(self, request):
        print(request.body)

        # TODO: differnet way of getting the project id
        project = Project.objects.filter(organization_id=ORG_ID, status=0).first()
        project_key = ProjectKey.get_default(project=project)
        endpoint = absolute_uri(
            "/api/%d/store/?sentry_key=%s" % (project.id, project_key.public_key)
        )
        print("endpoint", endpoint)

        for record in request.data["records"]:
            # decode and un-gzip data
            data = gunzip(b64decode(record["data"]))
            print("data", data)
            body = json.loads(data)
            session = http.build_session()

            for event in body["logEvents"]:
                arr = [line.strip() for line in event["message"].splitlines()]
                [message, exception_type] = arr[0].split(": ")
                prev_index = arr.index("Traceback (most recent call last):")
                frames = [line.strip() for line in arr[prev_index + 1].split(",")]

                payload = {
                    "event_id": uuid.uuid4().hex,
                    "message": {"message": message},
                    "exception": {"type": exception_type},
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": frames[0].lstrip("File").strip(),
                                "lineno": frames[1].lstrip("line").strip(),
                                "function": frames[2].lstrip("in").strip(),
                            }
                        ]
                    },
                }
                print("payload", payload)

                try:
                    resp = session.post(endpoint, json=payload)
                    json_error = resp.json()
                    resp.raise_for_status()
                except RequestException as e:
                    # errors here should be uncommon but we should be aware of them
                    logger.error(
                        "Error sending stacktrace from AWS Lambda to sentry: %s - %s"
                        % (e, json_error),
                        extra={
                            "project_id": project.id,
                            "project_public_key": project_key.public_key,
                        },
                        exc_info=True,
                    )
                    # 400 probably isn't the right status code but oh well
                    return self.respond(
                        {"detail": "Error sending stacktrace from AWS Lamda to sentry: %s" % e},
                        status=400,
                    )

        return self.respond(status=200)