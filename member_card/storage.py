#!/usr/bin/env python
import logging
from flask import current_app
from google.cloud import storage

from member_card.utils import load_gcp_credentials

logger = logging.getLogger(__name__)


def get_client(credentials=None):
    if credentials is None:
        credentials = load_gcp_credentials()
    return storage.Client(credentials=credentials)


def get_bucket(client=None):
    if client is None:
        client = get_client()
    return client.get_bucket(current_app.config["GCS_BUCKET_ID"])


def upload_file_to_gcs(local_file, remote_path, content_type=None):
    blob = get_bucket().blob(remote_path)
    if content_type is not None:
        blob.content_type = content_type
    blob.cache_control = "no-cache"

    logger.debug(f"Uploading {local_file=}) to {remote_path=}")

    blob.upload_from_filename(local_file)

    return blob


# from datetime import timedelta
# def get_presigned_url(blob, expiration: "timedelta"):
#     url = blob.generate_signed_url(
#         version="v4",
#         # This URL is valid for 15 minutes
#         expiration=expiration,
#         # Allow GET requests using this URL.
#         method="GET",
#         credentials=load_gcp_credentials(),
#     )
#     logger.info(f"GCS signed URL for {blob=}: {url=}")
#     return url
