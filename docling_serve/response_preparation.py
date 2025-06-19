import logging
import os
import shutil
import time
import zipfile
from collections.abc import Iterable
from io import TextIOWrapper
from pathlib import Path
from typing import Union

import obstore
from fastapi import HTTPException
from fastapi.responses import FileResponse

from docling.datamodel.base_models import OutputFormat
from docling.datamodel.document import ConversionResult, ConversionStatus
from docling_core.types.doc import ImageRefMode

from docling_serve.datamodel.convert import ConvertDocumentsOptions
from docling_serve.datamodel.responses import (
    ConvertDocumentResponse,
    DocumentResponse,
    RemoteConvertDocumentResult,
    RemoteConvertDocumentsResponse,
)

_log = logging.getLogger(__name__)


def _export_document_as_content(
    conv_res: ConversionResult,
    export_json: bool,
    export_html: bool,
    export_md: bool,
    export_txt: bool,
    export_doctags: bool,
    image_mode: ImageRefMode,
    md_page_break_placeholder: str,
):
    document = DocumentResponse(filename=conv_res.input.file.name)

    if conv_res.status == ConversionStatus.SUCCESS:
        new_doc = conv_res.document._make_copy_with_refmode(Path(), image_mode)

        # Create the different formats
        if export_json:
            document.json_content = new_doc
        if export_html:
            document.html_content = new_doc.export_to_html(image_mode=image_mode)
        if export_txt:
            document.text_content = new_doc.export_to_markdown(
                strict_text=True,
                image_mode=image_mode,
            )
        if export_md:
            document.md_content = new_doc.export_to_markdown(
                image_mode=image_mode,
                page_break_placeholder=md_page_break_placeholder or None,
            )
        if export_doctags:
            document.doctags_content = new_doc.export_to_doctags()
    elif conv_res.status == ConversionStatus.SKIPPED:
        raise HTTPException(status_code=400, detail=conv_res.errors)
    else:
        raise HTTPException(status_code=500, detail=conv_res.errors)

    return document


def _export_documents_as_files(
    conv_results: Iterable[ConversionResult],
    output_dir: Path,
    export_json: bool,
    export_html: bool,
    export_md: bool,
    export_txt: bool,
    export_doctags: bool,
    image_export_mode: ImageRefMode,
    md_page_break_placeholder: str,
):
    success_count = 0
    failure_count = 0

    for conv_res in conv_results:
        if conv_res.status == ConversionStatus.SUCCESS:
            success_count += 1
            doc_filename = conv_res.input.file.stem

            # Export JSON format:
            if export_json:
                fname = output_dir / f"{doc_filename}.json"
                _log.info(f"writing JSON output to {fname}")
                conv_res.document.save_as_json(
                    filename=fname, image_mode=image_export_mode
                )

            # Export HTML format:
            if export_html:
                fname = output_dir / f"{doc_filename}.html"
                _log.info(f"writing HTML output to {fname}")
                conv_res.document.save_as_html(
                    filename=fname, image_mode=image_export_mode
                )

            # Export Text format:
            if export_txt:
                fname = output_dir / f"{doc_filename}.txt"
                _log.info(f"writing TXT output to {fname}")
                conv_res.document.save_as_markdown(
                    filename=fname,
                    strict_text=True,
                    image_mode=ImageRefMode.PLACEHOLDER,
                )

            # Export Markdown format:
            if export_md:
                fname = output_dir / f"{doc_filename}.md"
                _log.info(f"writing Markdown output to {fname}")
                conv_res.document.save_as_markdown(
                    filename=fname,
                    image_mode=image_export_mode,
                    page_break_placeholder=md_page_break_placeholder or None,
                )

            # Export Document Tags format:
            if export_doctags:
                fname = output_dir / f"{doc_filename}.doctags"
                _log.info(f"writing Doc Tags output to {fname}")
                conv_res.document.save_as_document_tokens(filename=fname)

        else:
            _log.warning(f"Document {conv_res.input.file} failed to convert.")
            failure_count += 1

    _log.info(
        f"Processed {success_count + failure_count} docs, "
        f"of which {failure_count} failed"
    )


def process_results(
    conversion_options: ConvertDocumentsOptions,
    conv_results: Iterable[ConversionResult],
    work_dir: Path,
) -> Union[RemoteConvertDocumentsResponse, ConvertDocumentResponse, FileResponse]:
    # Let's start by processing the documents
    try:
        start_time = time.monotonic()

        # Convert the iterator to a list to count the number of results and get timings
        # As it's an iterator (lazy evaluation), it will also start the conversion
        conv_results = list(conv_results)

        processing_time = time.monotonic() - start_time

        _log.info(
            f"Processed {len(conv_results)} docs in {processing_time:.2f} seconds."
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if len(conv_results) == 0:
        raise HTTPException(
            status_code=500, detail="No documents were generated by Docling."
        )

    # We have some results, let's prepare the response
    response: Union[
        FileResponse, ConvertDocumentResponse, RemoteConvertDocumentsResponse
    ]

    # Booleans to know what to export
    export_json = OutputFormat.JSON in conversion_options.to_formats
    export_html = OutputFormat.HTML in conversion_options.to_formats
    export_md = OutputFormat.MARKDOWN in conversion_options.to_formats
    export_txt = OutputFormat.TEXT in conversion_options.to_formats
    export_doctags = OutputFormat.DOCTAGS in conversion_options.to_formats

    # Only 1 document was processed, and we are not returning it as a file or bucket
    if len(conv_results) == 1 and not (
        conversion_options.return_as_file or conversion_options.result_bucket_path
    ):
        conv_res = conv_results[0]
        document = _export_document_as_content(
            conv_res,
            export_json=export_json,
            export_html=export_html,
            export_md=export_md,
            export_txt=export_txt,
            export_doctags=export_doctags,
            image_mode=conversion_options.image_export_mode,
            md_page_break_placeholder=conversion_options.md_page_break_placeholder,
        )

        response = ConvertDocumentResponse(
            document=document,
            status=conv_res.status,
            processing_time=processing_time,
            timings=conv_res.timings,
        )

    elif conversion_options.result_bucket_path:
        store = obstore.store.from_url(str(conversion_options.result_bucket_path))
        # Create an empty response to fill with results
        response = RemoteConvertDocumentsResponse(
            results=[],
            processing_time=processing_time,
        )
        for conv_result in conv_results:
            document = _export_document_as_content(
                conv_result,
                export_json=export_json,
                export_html=export_html,
                export_md=export_md,
                export_txt=export_txt,
                export_doctags=export_doctags,
                image_mode=conversion_options.image_export_mode,
                md_page_break_placeholder=conversion_options.md_page_break_placeholder,
            )
            # Result archive will be called <filename>.zip
            rel_result_path = f"{conv_result.input.file.stem}.zip"
            with obstore.open_writer(store, rel_result_path) as raw_writer:
                with zipfile.ZipFile(raw_writer, "w") as zipf:
                    with zipf.open(f"{document.filename}.json", "w") as zip_member:
                        with TextIOWrapper(zip_member, encoding="utf-8") as text_stream:
                            text_stream.write(document.model_dump_json(indent=2))

            # Build a RemoteConvertDocumentResult for each conversion result, providing full bucket path to the result file
            base_uri = str(conversion_options.result_bucket_path).rstrip("/")
            result_uri = f"{base_uri}/{rel_result_path}"
            remote_result = RemoteConvertDocumentResult(
                result_uri=result_uri,
                status=conv_result.status,
                errors=conv_result.errors,
                timings=conv_result.timings,
            )
            # Append the remote result to the response
            response.results.append(remote_result)

    # Multiple documents were processed, or we are forced returning as a file
    else:
        # Temporary directory to store the outputs
        output_dir = work_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Worker pid to use in archive identification as we may have multiple workers
        os.getpid()

        # Export the documents
        _export_documents_as_files(
            conv_results=conv_results,
            output_dir=output_dir,
            export_json=export_json,
            export_html=export_html,
            export_md=export_md,
            export_txt=export_txt,
            export_doctags=export_doctags,
            image_export_mode=conversion_options.image_export_mode,
            md_page_break_placeholder=conversion_options.md_page_break_placeholder,
        )

        files = os.listdir(output_dir)
        if len(files) == 0:
            raise HTTPException(status_code=500, detail="No documents were exported.")

        file_path = work_dir / "converted_docs.zip"
        shutil.make_archive(
            base_name=str(file_path.with_suffix("")),
            format="zip",
            root_dir=output_dir,
        )

        # Other cleanups after the response is sent
        # Output directory
        # background_tasks.add_task(shutil.rmtree, work_dir, ignore_errors=True)

        response = FileResponse(
            file_path, filename=file_path.name, media_type="application/zip"
        )

    return response
