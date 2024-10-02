from pathlib import Path

from docling.datamodel.pipeline_options import (
    EasyOcrOptions,
    PipelineOptions,
    TesseractOcrOptions,
)
from docling.models.base_ocr_model import BaseOcrModel
from docling.models.easyocr_model import EasyOcrModel
from docling.models.layout_model import LayoutModel
from docling.models.table_structure_model import TableStructureModel
from docling.pipeline.base_model_pipeline import BaseModelPipeline


class StandardModelPipeline(BaseModelPipeline):
    _layout_model_path = "model_artifacts/layout/beehive_v0.0.5"
    _table_model_path = "model_artifacts/tableformer"

    def __init__(self, artifacts_path: Path, pipeline_options: PipelineOptions):
        super().__init__(artifacts_path, pipeline_options)

        ocr_model: BaseOcrModel
        if isinstance(pipeline_options.ocr_options, EasyOcrOptions):
            ocr_model = EasyOcrModel(
                enabled=pipeline_options.do_ocr,
                options=pipeline_options.ocr_options,
            )
        elif isinstance(pipeline_options.ocr_options, TesseractOcrOptions):
            raise NotImplemented()
            # TODO
            # ocr_model = TesseractOcrModel(
            #     enabled=pipeline_options.do_ocr,
            #     options=pipeline_options.ocr_options,
            # )
        else:
            raise RuntimeError(
                f"The specified OCR kind is not supported: {pipeline_options.ocr_options.kind}."
            )

        self.model_pipe = [
            # OCR
            ocr_model,
            # Layout
            LayoutModel(
                config={
                    "artifacts_path": artifacts_path
                    / StandardModelPipeline._layout_model_path
                }
            ),
            # Table structure
            TableStructureModel(
                config={
                    "artifacts_path": artifacts_path
                    / StandardModelPipeline._table_model_path,
                    "enabled": pipeline_options.do_table_structure,
                    "mode": pipeline_options.table_structure_options.mode,
                    "do_cell_matching": pipeline_options.table_structure_options.do_cell_matching,
                }
            ),
        ]
