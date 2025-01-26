import csv
import io
import logging
import os
import tempfile
from subprocess import DEVNULL, PIPE, Popen
from typing import Iterable, List, Optional, Tuple

import pandas as pd
from docling_core.types.doc import BoundingBox, CoordOrigin

from docling.datamodel.base_models import Cell, OcrCell, Page
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import TesseractCliOcrOptions
from docling.datamodel.settings import settings
from docling.models.base_ocr_model import BaseOcrModel
from docling.utils.ocr_utils import map_tesseract_script
from docling.utils.profiling import TimeRecorder

_log = logging.getLogger(__name__)


class TesseractOcrCliModel(BaseOcrModel):
    def __init__(self, enabled: bool, options: TesseractCliOcrOptions):
        super().__init__(enabled=enabled, options=options)
        self.options: TesseractCliOcrOptions

        self.scale = 3  # multiplier for 72 dpi == 216 dpi.

        self._name: Optional[str] = None
        self._version: Optional[str] = None
        self._tesseract_languages: Optional[List[str]] = None
        self._script_prefix: Optional[str] = None

        if self.enabled:
            try:
                self._get_name_and_version()
                self._set_languages_and_prefix()

            except Exception as exc:
                raise RuntimeError(
                    f"Tesseract is not available, aborting: {exc} "
                    "Install tesseract on your system and the tesseract binary is discoverable. "
                    "The actual command for Tesseract can be specified in `pipeline_options.ocr_options.tesseract_cmd='tesseract'`. "
                    "Alternatively, Docling has support for other OCR engines. See the documentation."
                )

    def _get_name_and_version(self) -> Tuple[str, str]:

        if self._name != None and self._version != None:
            return self._name, self._version  # type: ignore

        cmd = [self.options.tesseract_cmd, "--version"]

        proc = Popen(cmd, stdout=PIPE, stderr=PIPE)
        stdout, stderr = proc.communicate()

        proc.wait()

        # HACK: Windows versions of Tesseract output the version to stdout, Linux versions
        # to stderr, so check both.
        version_line = (
            (stdout.decode("utf8").strip() or stderr.decode("utf8").strip())
            .split("\n")[0]
            .strip()
        )

        # If everything else fails...
        if not version_line:
            version_line = "tesseract XXX"

        name, version = version_line.split(" ")

        self._name = name
        self._version = version

        return name, version

    def _run_tesseract(self, ifilename: str):
        r"""
        Run tesseract CLI
        """
        cmd = [self.options.tesseract_cmd]

        if "auto" in self.options.lang:
            lang = self._detect_language(ifilename)
            if lang is not None:
                cmd.append("-l")
                cmd.append(lang)
        elif self.options.lang is not None and len(self.options.lang) > 0:
            cmd.append("-l")
            cmd.append("+".join(self.options.lang))

        if self.options.path is not None:
            cmd.append("--tessdata-dir")
            cmd.append(self.options.path)

        cmd += [ifilename, "stdout", "tsv"]
        _log.info("command: {}".format(" ".join(cmd)))

        proc = Popen(cmd, stdout=PIPE, stderr=DEVNULL)
        output, _ = proc.communicate()

        # _log.info(output)

        # Decode the byte string to a regular string
        decoded_data = output.decode("utf-8")
        # _log.info(decoded_data)

        # Read the TSV file generated by Tesseract
        df = pd.read_csv(io.StringIO(decoded_data), quoting=csv.QUOTE_NONE, sep="\t")

        # Display the dataframe (optional)
        # _log.info("df: ", df.head())

        # Filter rows that contain actual text (ignore header or empty rows)
        df_filtered = df[df["text"].notnull() & (df["text"].str.strip() != "")]

        return df_filtered

    def _detect_language(self, ifilename: str):
        r"""
        Run tesseract in PSM 0 mode to detect the language
        """
        assert self._tesseract_languages is not None

        cmd = [self.options.tesseract_cmd]
        cmd.extend(["--psm", "0", "-l", "osd", ifilename, "stdout"])
        _log.info("command: {}".format(" ".join(cmd)))
        proc = Popen(cmd, stdout=PIPE, stderr=DEVNULL)
        output, _ = proc.communicate()
        decoded_data = output.decode("utf-8")
        df = pd.read_csv(
            io.StringIO(decoded_data), sep=":", header=None, names=["key", "value"]
        )
        scripts = df.loc[df["key"] == "Script"].value.tolist()
        if len(scripts) == 0:
            _log.warning("Tesseract cannot detect the script of the page")
            return None

        script = map_tesseract_script(scripts[0].strip())
        lang = f"{self._script_prefix}{script}"

        # Check if the detected language has been installed
        if lang not in self._tesseract_languages:
            msg = f"Tesseract detected the script '{script}' and language '{lang}'."
            msg += " However this language is not installed in your system and will be ignored."
            _log.warning(msg)
            return None

        _log.debug(
            f"Using tesseract model for the detected script '{script}' and language '{lang}'"
        )
        return lang

    def _set_languages_and_prefix(self):
        r"""
        Read and set the languages installed in tesseract and decide the script prefix
        """
        # Get all languages
        cmd = [self.options.tesseract_cmd]
        cmd.append("--list-langs")
        _log.info("command: {}".format(" ".join(cmd)))
        proc = Popen(cmd, stdout=PIPE, stderr=DEVNULL)
        output, _ = proc.communicate()
        decoded_data = output.decode("utf-8")
        df = pd.read_csv(io.StringIO(decoded_data), header=None)
        self._tesseract_languages = df[0].tolist()[1:]

        # Decide the script prefix
        if any([l.startswith("script/") for l in self._tesseract_languages]):
            script_prefix = "script/"
        else:
            script_prefix = ""

        self._script_prefix = script_prefix

    def __call__(
        self, conv_res: ConversionResult, page_batch: Iterable[Page]
    ) -> Iterable[Page]:

        if not self.enabled:
            yield from page_batch
            return

        for page in page_batch:
            assert page._backend is not None
            if not page._backend.is_valid():
                yield page
            else:
                with TimeRecorder(conv_res, "ocr"):
                    ocr_rects = self.get_ocr_rects(page)

                    all_ocr_cells = []
                    for ocr_rect in ocr_rects:
                        # Skip zero area boxes
                        if ocr_rect.area() == 0:
                            continue
                        high_res_image = page._backend.get_page_image(
                            scale=self.scale, cropbox=ocr_rect
                        )
                        try:
                            with tempfile.NamedTemporaryFile(
                                suffix=".png", mode="w+b", delete=False
                            ) as image_file:
                                fname = image_file.name
                                high_res_image.save(image_file)

                            df = self._run_tesseract(fname)
                        finally:
                            if os.path.exists(fname):
                                os.remove(fname)

                        # _log.info(df)

                        # Print relevant columns (bounding box and text)
                        for ix, row in df.iterrows():
                            text = row["text"]
                            conf = row["conf"]

                            l = float(row["left"])
                            b = float(row["top"])
                            w = float(row["width"])
                            h = float(row["height"])

                            t = b + h
                            r = l + w

                            cell = OcrCell(
                                id=ix,
                                text=text,
                                confidence=conf / 100.0,
                                bbox=BoundingBox.from_tuple(
                                    coord=(
                                        (l / self.scale) + ocr_rect.l,
                                        (b / self.scale) + ocr_rect.t,
                                        (r / self.scale) + ocr_rect.l,
                                        (t / self.scale) + ocr_rect.t,
                                    ),
                                    origin=CoordOrigin.TOPLEFT,
                                ),
                            )
                            all_ocr_cells.append(cell)

                    # Post-process the cells
                    page.cells = self.post_process_cells(all_ocr_cells, page.cells)

                # DEBUG code:
                if settings.debug.visualize_ocr:
                    self.draw_ocr_rects_and_cells(conv_res, page, ocr_rects)

                yield page
