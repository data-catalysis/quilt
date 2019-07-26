"""
phone data into elastic for supported file extensions.
note: we truncate outbound documents to DOC_SIZE_LIMIT characters
(to bound memory pressure and request size to elastic)
"""

from datetime import datetime
from math import floor
import json
import os
from urllib.parse import unquote, unquote_plus

from aws_requests_auth.aws_auth import AWSRequestsAuth
import boto3
import botocore
from elasticsearch import Elasticsearch, RequestsHttpConnection
from elasticsearch.helpers import bulk
import nbformat
from tenacity import stop_after_attempt, stop_after_delay, retry, wait_exponential

CONTENT_INDEX_EXTS = [
    ".csv",
    ".html",
    ".ipynb",
    ".json",
    ".md",
    ".rmd",
    ".tsv",
    ".txt",
    ".xml"
]
# 10 MB, see https://amzn.to/2xJpngN
CHUNK_LIMIT_BYTES = 20_000_000
DOC_LIMIT_BYTES = 10_000
ELASTIC_TIMEOUT = 30
MAX_RETRY = 10 # prevent long-running lambdas due to malformed calls
NB_VERSION = 4 # default notebook version for nbformat
# signifies that the object is truly deleted, not to be confused with
# s3:ObjectRemoved:DeleteMarkerCreated, which we may see in versioned buckets
# see https://docs.aws.amazon.com/AmazonS3/latest/dev/NotificationHowTo.html
OBJECT_DELETE = "ObjectRemoved:Delete"
QUEUE_LIMIT_BYTES = 100_000_000# 100MB
RETRY_429 = 5
TEST_EVENT = "s3:TestEvent"
# we need to filter out GetObject and HeadObject calls generated by the present
#  lambda in order to display accurate analytics in the Quilt catalog
#  a custom user agent enables said filtration
USER_AGENT_EXTRA = " quilt3-lambdas-es-indexer"

def bulk_send(elastic, list_):
    """make a bulk() call to elastic"""
    return bulk(
        elastic,
        list_,
        # Some magic numbers to reduce memory pressure
        # e.g. see https://github.com/wagtail/wagtail/issues/4554
        chunk_size=100,# max number of documents sent in one chunk
        # The stated default is max_chunk_bytes=10485760, but with default
        # ES will still return an exception stating that the very
        # same request size limit has been exceeded
        max_chunk_bytes=CHUNK_LIMIT_BYTES,
        # number of retries for 429 (too many requests only)
        # all other errors handled by our code
        max_retries=RETRY_429,
        # we'll process errors on our own
        raise_on_error=False,
        raise_on_exception=False
    )

def make_fulltext_string(text, key):
    """make a string for fulltext indexing"""
    base, ext = os.path.splitext(key)
    key_parts = base.split("/").append(ext[1:])

    return f"{text} {key} {key_parts.join(' ')}"

class DocumentQueue:
    """transient in-memory queue for documents to be indexed"""
    def __init__(self, context):
        """constructor"""
        self.queue = []
        self.size = 0
        self.context = context

    def append(
            self,
            event_type,
            size=0,
            meta=None,
            *,
            last_modified,
            bucket,
            ext,
            key,
            text,
            etag,
            version_id
    ):
        """format event as a document and then queue the document"""
        # On types and fields, see
        # https://www.elastic.co/guide/en/elasticsearch/reference/master/mapping.html
        body = {
            # Elastic native keys
            "_id": f"{key}:{version_id}",
            "_index": bucket,
            # index will upsert (and clobber existing equivalent _ids)
            "_op_type": "delete" if event_type == OBJECT_DELETE else "index",
            "_type": "_doc",
            # Quilt keys
            # Be VERY CAREFUL changing these values, as a type change can cause a
            # mapper_parsing_exception that below code won't handle
            "etag": etag,
            "ext": ext,
            "event": event_type,
            "size": size,
            "text": make_fulltext_string(text, key),
            "key": key,
            "last_modified": last_modified.isoformat(),
            "updated": datetime.utcnow().isoformat(),
            "version_id": version_id
        }

        body = {**body, **transform_meta(meta or {})}

        body["meta_text"] = " ".join([body["meta_text"], key])

        self.append_document(body)

        if self.size >= QUEUE_LIMIT_BYTES:
            self.send_all()

    def append_document(self, doc):
        """append well-formed documents (used for retry or by append())"""
        if doc["text"]:
            # document text dominates memory footprint; OK to neglect the
            # small fixed size for the JSON metadata
            self.size += min(doc["size"], DOC_LIMIT_BYTES)
        self.queue.append(doc)

    def send_all(self):
        """flush self.queue in 1-2 bulk calls"""
        if  not self.queue:
            return
        elastic_host = os.environ["ES_HOST"]
        session = boto3.session.Session()
        credentials = session.get_credentials().get_frozen_credentials()
        awsauth = AWSRequestsAuth(
            # These environment variables are automatically set by Lambda
            aws_access_key=credentials.access_key,
            aws_secret_access_key=credentials.secret_key,
            aws_token=credentials.token,
            aws_host=elastic_host,
            aws_region=session.region_name,
            aws_service="es"
        )

        elastic = Elasticsearch(
            hosts=[{"host": elastic_host, "port": 443}],
            http_auth=awsauth,
            max_backoff=get_time_remaining(self.context),
            # Give ES time to respond when under load
            timeout=ELASTIC_TIMEOUT,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection
        )

        _, errors = bulk_send(elastic, self.queue)
        if errors:
            id_to_doc = {d["_id"]: d for d in self.queue}
            send_again = []
            for error in errors:
                handled = False
                # only retry index call errors, not delete errors
                if "index" in error:
                    inner = error["index"]
                    info = inner.get("error")
                    # because error.error might be a string *sigh*
                    if isinstance(info, dict):
                        if "mapper_parsing_exception" in info.get("type", ""):
                            print("mapper_parsing_exception", error, inner)
                            doc = id_to_doc[inner["_id"]]
                            # clear out structured metadata and try again
                            doc["user_meta"] = doc["system"] = {}
                            handled = True
                            send_again.append(doc)
                if not handled:
                    print("unhandled indexer error:", error)
            # we won't retry after this (elasticsearch might retry 429s tho)
            if send_again:
                bulk_send(elastic, send_again)
            # empty the queue
        self.size = 0
        self.queue = []

def get_contents(context, bucket, key, ext, *, etag, version_id, s3_client, size):
    """get the byte contents of a file"""
    content = ""
    if ext in CONTENT_INDEX_EXTS:
        if ext == ".ipynb":
            content = trim_to_bytes(
                # we have no choice but to fetch the entire notebook, because we
                # are going to parse it
                # warning: huge notebooks could spike memory here
                get_notebook_cells(
                    context,
                    bucket,
                    key,
                    size,
                    etag=etag,
                    s3_client=s3_client,
                    version_id=version_id
                )
            )
            content = trim_to_bytes(content)
        else:
            content = get_plain_text(
                context,
                bucket,
                key,
                size,
                etag=etag,
                s3_client=s3_client,
                version_id=version_id
            )

    return content

def extract_text(notebook_str):
    """ Extract code and markdown
    Args:
        * nb - notebook as a string
    Returns:
        * str - select code and markdown source (and outputs)
    Pre:
        * notebook is well-formed per notebook version 4
        * "cell_type" is defined for all cells
        * "source" defined for all "code" and "markdown" cells
    Throws:
        * Anything nbformat.reads() can throw :( which is diverse and poorly
        documented, hence the `except Exception` in handler()
    Notes:
        * Deliberately decided not to index output streams and display strings
        because they were noisy and low value
        * Tested this code against ~6400 Jupyter notebooks in
        s3://alpha-quilt-storage/tree/notebook-search/
        * Might be useful to index "cell_type" : "raw" in the future
    See also:
        * Format reference https://nbformat.readthedocs.io/en/latest/format_description.html
    """
    formatted = nbformat.reads(notebook_str, as_version=NB_VERSION)
    text = []
    for cell in formatted.get("cells", []):
        if "source" in cell and cell.get("cell_type") in ("code", "markdown"):
            text.append(cell["source"])

    return "\n".join(text)

def get_notebook_cells(context, bucket, key, size, *, etag, s3_client, version_id):
    """extract cells for ipynb notebooks for indexing"""
    text = ""
    try:
        obj = retry_s3(
            "get",
            context,
            bucket,
            key,
            size,
            etag=etag,
            s3_client=s3_client,
            version_id=version_id
        )
        notebook = obj["Body"].read().decode("utf-8")
        text = extract_text(notebook)
    except UnicodeDecodeError as uni:
        print(f"Unicode decode error in {key}: {uni}")
    except (json.JSONDecodeError, nbformat.reader.NotJSONError):
        print(f"Invalid JSON in {key}.")
    except (KeyError, AttributeError)  as err:
        print(f"Missing key in {key}: {err}")
    # there might be more errors than covered by test_read_notebook
    # better not to fail altogether
    except Exception as exc:#pylint: disable=broad-except
        print(f"Exception in file {key}: {exc}")

    return text

def get_plain_text(context, bucket, key, size, *, etag, s3_client, version_id):
    """get plain text object contents"""
    text = ""
    try:
        obj = retry_s3(
            "get",
            context,
            bucket,
            key,
            size,
            etag=etag,
            s3_client=s3_client,
            limit=DOC_LIMIT_BYTES,
            version_id=version_id
        )
        # ignore because limit might break a long character midstream
        text = obj["Body"].read().decode("utf-8", "ignore")
    except UnicodeDecodeError as ex:
        print(f"Unicode decode error in {key}", ex)

    return text

def get_time_remaining(context):
    """returns time remaining in seconds before lambda context is shut down"""
    time_remaining = floor(context.get_remaining_time_in_millis()/1000)
    if time_remaining < 30:
        print(
            f"Warning: Lambda function has less than {time_remaining} seconds."
            " Consider reducing bulk batch size."
        )

    return time_remaining

def make_s3_client():
    """make a client with a custom user agent string so that we can
    filter the present lambda's requests to S3 from object analytics"""
    configuration = botocore.config.Config(user_agent_extra=USER_AGENT_EXTRA)
    return boto3.client("s3", config=configuration)

def transform_meta(meta):
    """ Reshapes metadata for indexing in ES """
    helium = meta.get("helium", {})
    user_meta = helium.pop("user_meta", {})
    comment = helium.pop("comment", "")
    target = helium.pop("target", "")

    meta_text_parts = [comment, target]

    if helium:
        meta_text_parts.append(json.dumps(helium))
    if user_meta:
        meta_text_parts.append(json.dumps(user_meta))

    return {
        "system_meta": helium,
        "user_meta": user_meta,
        "comment": comment,
        "target": target,
        "meta_text": " ".join(meta_text_parts)
    }

def handler(event, context):
    """enumerate S3 keys in event, extract relevant data and metadata,
    queue events, send to elastic via bulk() API
    """
    # message is a proper SQS message, which either contains a single event
    # (from the bucket notification system) or batch-many events as determined
    # by enterprise/**/bulk_loader.py
    for message in event["Records"]:
        body = json.loads(message["body"])
        body_message = json.loads(body["Message"])
        if "Records" not in body_message:
            if body_message.get("Event") == TEST_EVENT:
                # Consume and ignore this event, which is an initial message from
                # SQS; see https://forums.aws.amazon.com/thread.jspa?threadID=84331
                continue
            else:
                print("Unexpected message['body']. No 'Records' key.", message)
        batch_processor = DocumentQueue(context)
        events = body_message.get("Records", [])
        s3_client = make_s3_client()
        # event is a single S3 event
        for event_ in events:
            try:
                event_name = event_["eventName"]
                bucket = unquote(event_["s3"]["bucket"]["name"])
                # In the grand tradition of IE6, S3 events turn spaces into '+'
                key = unquote_plus(event_["s3"]["object"]["key"])
                version_id = event_["s3"]["object"].get("versionId")
                version_id = unquote(version_id) if version_id else None
                etag = unquote(event_["s3"]["object"]["eTag"])
                _, ext = os.path.splitext(key)
                ext = ext.lower()

                head = retry_s3(
                    "head",
                    context,
                    bucket,
                    key,
                    s3_client=s3_client,
                    version_id=version_id,
                    etag=etag
                )

                size = head["ContentLength"]
                last_modified = head["LastModified"]
                meta = head["Metadata"]
                text = ""

                if event_name == OBJECT_DELETE:
                    batch_processor.append(
                        event_name,
                        bucket=bucket,
                        ext=ext,
                        etag=etag,
                        key=key,
                        last_modified=last_modified,
                        text=text,
                        version_id=version_id
                    )
                    continue

                _, ext = os.path.splitext(key)
                ext = ext.lower()
                text = get_contents(
                    context,
                    bucket,
                    key,
                    ext,
                    etag=etag,
                    version_id=version_id,
                    s3_client=s3_client,
                    size=size
                )
                # decode Quilt-specific metadata
                try:
                    if "helium" in meta:
                        meta["helium"] = json.loads(meta["helium"])
                except (KeyError, json.JSONDecodeError):
                    print("Unable to parse Quilt 'helium' metadata", meta)

                batch_processor.append(
                    event_name,
                    bucket=bucket,
                    key=key,
                    ext=ext,
                    meta=meta,
                    etag=etag,
                    version_id=version_id,
                    last_modified=last_modified,
                    size=size,
                    text=text
                )
            except Exception as exc:# pylint: disable=broad-except
                print("Fatal exception for record", event_, exc)
                import traceback
                traceback.print_tb(exc.__traceback__)
        # flush the queue
        batch_processor.send_all()

def retry_s3(
        operation,
        context,
        bucket,
        key,
        size=None,
        limit=None,
        *,
        etag,
        version_id,
        s3_client
):
    """retry head or get operation to S3 with; stop before we run out of time.
    retry is necessary since, due to eventual consistency, we may not
    always get the required version of the object.
    """
    if operation == "head":
        function_ = s3_client.head_object
    elif operation == "get":
        function_ = s3_client.get_object
    else:
        raise ValueError(f"unexpected operation: {operation}")
    # Keyword arguments to function_
    arguments = {
        "Bucket": bucket,
        "Key": key
    }
    if operation == 'get' and size:
        # can only request range if file is not empty
        arguments['Range'] = f"bytes=0-{limit}"
    if version_id:
        arguments['VersionId'] = version_id
    else:
        arguments['IfMatch'] = etag

    time_remaining = get_time_remaining(context)
    @retry(
        # debug
        stop=(stop_after_delay(time_remaining) | stop_after_attempt(MAX_RETRY)),
        wait=wait_exponential(multiplier=2, min=4, max=30)
    )
    def call():
        """local function so we can set stop_after_delay dynamically"""
        return function_(**arguments)

    return call()

def trim_to_bytes(string, limit=DOC_LIMIT_BYTES):
    """trim string to specified number of bytes"""
    encoded = string.encode("utf-8")
    size = len(encoded)
    if size <= limit:
        return string
    return encoded[:limit].decode("utf-8", "ignore")
