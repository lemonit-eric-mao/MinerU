import time

import cv2
import numpy as np
import torch
from loguru import logger
from PIL import Image

from magic_pdf.config.constants import MODEL_NAME
from magic_pdf.config.exceptions import CUDA_NOT_AVAILABLE
from magic_pdf.data.dataset import Dataset
from magic_pdf.libs.clean_memory import clean_memory
from magic_pdf.model.doc_analyze_by_custom_model import ModelSingleton
from magic_pdf.model.operators import InferenceResult
from magic_pdf.model.pdf_extract_kit import CustomPEKModel
from magic_pdf.model.sub_modules.model_utils import (
    clean_vram,
    crop_img,
    get_res_list_from_layout_res,
)
from magic_pdf.model.sub_modules.ocr.paddleocr.ocr_utils import (
    get_adjusted_mfdetrec_res,
    get_ocr_result_list,
)

YOLO_LAYOUT_BASE_BATCH_SIZE = 4
MFD_BASE_BATCH_SIZE = 1
MFR_BASE_BATCH_SIZE = 16


class BatchAnalyze:
    def __init__(self, model: CustomPEKModel, batch_ratio: int):
        self.model = model
        self.batch_ratio = batch_ratio

    def __call__(self, images: list) -> list:
        if self.model.layout_model_name == MODEL_NAME.LAYOUTLMv3:
            # layoutlmv3
            images_layout_res = []
            for image in images:
                layout_res = self.model.layout_model(image, ignore_catids=[])
                images_layout_res.append(layout_res)
        elif self.model.layout_model_name == MODEL_NAME.DocLayout_YOLO:
            # doclayout_yolo
            images_layout_res = self.model.layout_model.batch_predict(
                images, self.batch_ratio * YOLO_LAYOUT_BASE_BATCH_SIZE
            )

        if self.model.apply_formula:
            # 公式检测
            images_mfd_res = self.model.mfd_model.batch_predict(
                images, self.batch_ratio * MFD_BASE_BATCH_SIZE
            )

            # 公式识别
            images_formula_list = self.model.mfr_model.batch_predict(
                images_mfd_res,
                images,
                batch_size=self.batch_ratio * MFR_BASE_BATCH_SIZE,
            )
            for image_index in range(len(images)):
                images_layout_res[image_index] += images_formula_list[image_index]

        # 清理显存
        clean_vram(self.model.device, vram_threshold=8)

        # reference: magic_pdf/model/doc_analyze_by_custom_model.py:doc_analyze
        for index in range(len(images)):
            layout_res = images_layout_res[index]
            pil_img = Image.fromarray(images[index])

            ocr_res_list, table_res_list, single_page_mfdetrec_res = (
                get_res_list_from_layout_res(layout_res)
            )
            # ocr识别
            ocr_start = time.time()
            # Process each area that requires OCR processing
            for res in ocr_res_list:
                new_image, useful_list = crop_img(
                    res, pil_img, crop_paste_x=50, crop_paste_y=50
                )
                adjusted_mfdetrec_res = get_adjusted_mfdetrec_res(
                    single_page_mfdetrec_res, useful_list
                )

                # OCR recognition
                new_image = cv2.cvtColor(np.asarray(new_image), cv2.COLOR_RGB2BGR)

                if self.model.apply_ocr:
                    ocr_res = self.model.ocr_model.ocr(
                        new_image, mfd_res=adjusted_mfdetrec_res
                    )[0]
                else:
                    ocr_res = self.model.ocr_model.ocr(
                        new_image, mfd_res=adjusted_mfdetrec_res, rec=False
                    )[0]

                # Integration results
                if ocr_res:
                    ocr_result_list = get_ocr_result_list(ocr_res, useful_list)
                    layout_res.extend(ocr_result_list)

            ocr_cost = round(time.time() - ocr_start, 2)
            if self.model.apply_ocr:
                logger.info(f"ocr time: {ocr_cost}")
            else:
                logger.info(f"det time: {ocr_cost}")

            # 表格识别 table recognition
            if self.model.apply_table:
                table_start = time.time()
                for res in table_res_list:
                    new_image, _ = crop_img(res, pil_img)
                    single_table_start_time = time.time()
                    html_code = None
                    if self.model.table_model_name == MODEL_NAME.STRUCT_EQTABLE:
                        with torch.no_grad():
                            table_result = self.model.table_model.predict(
                                new_image, "html"
                            )
                            if len(table_result) > 0:
                                html_code = table_result[0]
                    elif self.model.table_model_name == MODEL_NAME.TABLE_MASTER:
                        html_code = self.model.table_model.img2html(new_image)
                    elif self.model.table_model_name == MODEL_NAME.RAPID_TABLE:
                        html_code, table_cell_bboxes, elapse = (
                            self.model.table_model.predict(new_image)
                        )
                    run_time = time.time() - single_table_start_time
                    if run_time > self.model.table_max_time:
                        logger.warning(
                            f"table recognition processing exceeds max time {self.model.table_max_time}s"
                        )
                    # 判断是否返回正常
                    if html_code:
                        expected_ending = html_code.strip().endswith(
                            "</html>"
                        ) or html_code.strip().endswith("</table>")
                        if expected_ending:
                            res["html"] = html_code
                        else:
                            logger.warning(
                                "table recognition processing fails, not found expected HTML table end"
                            )
                    else:
                        logger.warning(
                            "table recognition processing fails, not get html return"
                        )
                logger.info(f"table time: {round(time.time() - table_start, 2)}")


def doc_batch_analyze(
    dataset: Dataset,
    ocr: bool = False,
    show_log: bool = False,
    start_page_id=0,
    end_page_id=None,
    lang=None,
    layout_model=None,
    formula_enable=None,
    table_enable=None,
    batch_ratio: int | None = None,
) -> InferenceResult:
    """
    Perform batch analysis on a document dataset.

    Args:
        dataset (Dataset): The dataset containing document pages to be analyzed.
        ocr (bool, optional): Flag to enable OCR (Optical Character Recognition). Defaults to False.
        show_log (bool, optional): Flag to enable logging. Defaults to False.
        start_page_id (int, optional): The starting page ID for analysis. Defaults to 0.
        end_page_id (int, optional): The ending page ID for analysis. Defaults to None, which means analyze till the last page.
        lang (str, optional): Language for OCR. Defaults to None.
        layout_model (optional): Layout model to be used for analysis. Defaults to None.
        formula_enable (optional): Flag to enable formula detection. Defaults to None.
        table_enable (optional): Flag to enable table detection. Defaults to None.
        batch_ratio (int | None, optional): Ratio for batch processing. Defaults to None, which sets it to 1.

    Raises:
        CUDA_NOT_AVAILABLE: If CUDA is not available, raises an exception as batch analysis is not supported in CPU mode.

    Returns:
        InferenceResult: The result of the batch analysis containing the analyzed data and the dataset.
    """

    if not torch.cuda.is_available():
        raise CUDA_NOT_AVAILABLE("batch analyze not support in CPU mode")

    lang = None if lang == "" else lang
    # TODO: auto detect batch size
    batch_ratio = 1 if batch_ratio is None else batch_ratio
    end_page_id = end_page_id if end_page_id else len(dataset)

    model_manager = ModelSingleton()
    custom_model: CustomPEKModel = model_manager.get_model(
        ocr, show_log, lang, layout_model, formula_enable, table_enable
    )
    batch_model = BatchAnalyze(model=custom_model, batch_ratio=batch_ratio)

    model_json = []

    # batch analyze
    images = []
    for index in range(len(dataset)):
        if start_page_id <= index <= end_page_id:
            page_data = dataset.get_page(index)
            img_dict = page_data.get_image()
            images.append(img_dict["img"])
    analyze_result = batch_model(images)

    for index in range(len(dataset)):
        page_data = dataset.get_page(index)
        img_dict = page_data.get_image()
        page_width = img_dict["width"]
        page_height = img_dict["height"]
        if start_page_id <= index <= end_page_id:
            result = analyze_result.pop(0)
        else:
            result = []

        page_info = {"page_no": index, "height": page_height, "width": page_width}
        page_dict = {"layout_dets": result, "page_info": page_info}
        model_json.append(page_dict)

    # TODO: clean memory when gpu memory is not enough
    clean_memory()

    return InferenceResult(model_json, dataset)
