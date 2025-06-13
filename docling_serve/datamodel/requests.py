import base64
from io import BytesIO
from typing import Annotated, Any, Union
from urllib.parse import unquote, urlparse

import obstore
from pydantic import AnyHttpUrl, AnyUrl, BaseModel, Field

from docling.datamodel.base_models import DocumentStream

from docling_serve.datamodel.convert import ConvertDocumentsOptions


class DocumentsConvertBase(BaseModel):
    options: ConvertDocumentsOptions = ConvertDocumentsOptions()


class BucketSource(BaseModel):
    uri: Annotated[
        AnyUrl,
        Field(
            description="Cloud storage bucket url to process",
            examples=["s3://bucket-name/path/to/file.pdf"],
        ),
    ]

    def to_document_stream(self) -> DocumentStream:
        parsed_url = urlparse(str(self.uri))
        file_path = unquote(parsed_url.path.lstrip("/"))
        if not file_path:
            raise ValueError(f"No path specified in the bucket URL: {self.uri}")

        # TODO - explicit credential handling.  obstore will just use cred provider defaults (env vars etc)
        store = obstore.store.from_url(parsed_url.scheme + "://" + parsed_url.netloc)
        if not store:
            raise ValueError(f"Invalid bucket URL: {self.uri}")

        reader = obstore.open_reader(store, file_path)
        buf = reader.read()
        return DocumentStream(stream=BytesIO(buf), name=file_path)


class HttpSource(BaseModel):
    url: Annotated[
        AnyHttpUrl,
        Field(
            description="HTTP url to process",
            examples=["https://arxiv.org/pdf/2206.01062"],
        ),
    ]
    headers: Annotated[
        dict[str, Any],
        Field(
            description="Additional headers used to fetch the urls, "
            "e.g. authorization, agent, etc"
        ),
    ] = {}


class FileSource(BaseModel):
    base64_string: Annotated[
        str,
        Field(
            description="Content of the file serialized in base64. "
            "For example it can be obtained via "
            "`base64 -w 0 /path/to/file/pdf-to-convert.pdf`."
        ),
    ]
    filename: Annotated[
        str,
        Field(description="Filename of the uploaded document", examples=["file.pdf"]),
    ]

    def to_document_stream(self) -> DocumentStream:
        buf = BytesIO(base64.b64decode(self.base64_string))
        return DocumentStream(stream=buf, name=self.filename)


class ConvertDocumentBucketSourcesRequest(DocumentsConvertBase):
    bucket_sources: list[BucketSource]


class ConvertDocumentHttpSourcesRequest(DocumentsConvertBase):
    http_sources: list[HttpSource]


class ConvertDocumentFileSourcesRequest(DocumentsConvertBase):
    file_sources: list[FileSource]


ConvertDocumentsRequest = Union[
    ConvertDocumentBucketSourcesRequest,
    ConvertDocumentFileSourcesRequest,
    ConvertDocumentHttpSourcesRequest,
]
