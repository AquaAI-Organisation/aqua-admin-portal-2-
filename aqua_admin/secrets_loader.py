import json
import os
import boto3
from dotenv import load_dotenv
load_dotenv()

SECRET_NAME = os.getenv("AWS_SECRET_NAME", "aqua_backend")
REGION = os.getenv("AWS_REGION", "eu-west-2")

def load_aws_secrets():
    client = boto3.client(
        "secretsmanager",
        region_name=REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )
    response = client.get_secret_value(
        SecretId=SECRET_NAME
    )
    secrets = json.loads(
        response["SecretString"]
    )
    for key, value in secrets.items():
        os.environ[key] = str(value)
        
    return secrets