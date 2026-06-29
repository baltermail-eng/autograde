from __future__ import annotations

import argparse
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from difflib import SequenceMatcher

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps
except ImportError:  # pragma: no cover - depends on local image environment
    Image = None
    ImageDraw = None
    ImageFilter = None
    ImageOps = None
try:
    import pytesseract
except ImportError:  # pragma: no cover - depends on local OCR environment
    pytesseract = None

# 如果是 Apple Silicon (M1/M2/M3) 且遇到 Tesseract 找不到的报错，可取消下行注释：
# if pytesseract is not None:
#     pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
DEFAULT_OCR_LANG = "chi_sim+eng"
DEFAULT_PSM_MODES = (6, 11)
DEFAULT_FUZZY_THRESHOLD = 0.9
DEFAULT_MIN_FUZZY_LENGTH = 6
OCR_CONFUSABLES = str.maketrans({
    "|": "1",
    "!": "1",
    "i": "1",
    "l": "1",
    "o": "0",
})


@dataclass(frozen=True)
class OcrToken:
    text: str
    normalized: str
    box: tuple[int, int, int, int]
    line_key: tuple[int, int, int, int]


@dataclass
class ScanResult:
    triggered_words: list[str]
    boxes: list[tuple[int, int, int, int]]
    redacted: bool = False


def normalize_text(text: str) -> str:
    """Normalize OCR/sensitive text for case-insensitive and whitespace-insensitive matching."""
    return "".join((text or "").lower().split())


def fuzzy_similarity(candidate: str, sensitive: str) -> float:
    raw = SequenceMatcher(None, candidate, sensitive).ratio()
    canonical_candidate = candidate.translate(OCR_CONFUSABLES)
    canonical_sensitive = sensitive.translate(OCR_CONFUSABLES)
    canonical = SequenceMatcher(None, canonical_candidate, canonical_sensitive).ratio()
    return max(raw, canonical)


def _resample_filter(name: str):
    if Image is None:
        return None
    resampling = getattr(Image, "Resampling", Image)
    return getattr(resampling, name)


def load_sensitive_words(txt_path: str | Path) -> dict[str, str]:
    """读取敏感词文件，返回 normalized -> 原始词。"""
    path = Path(txt_path)
    words: dict[str, str] = {}
    if not path.exists():
        print(f"错误：未找到敏感词文件 -> {path}")
        return words

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for part in line.split():
                normalized = normalize_text(part)
                if normalized:
                    words.setdefault(normalized, part)
    return words


def _int_at(data: dict[str, list], key: str, index: int) -> int:
    try:
        return int(float(data[key][index]))
    except (KeyError, TypeError, ValueError):
        return 0


def build_ocr_variants(img: Image.Image) -> list[tuple[str, Image.Image, float, float]]:
    """Build several OCR-oriented variants for screenshots and moire-heavy phone photos."""
    if Image is None or ImageFilter is None or ImageOps is None:
        raise RuntimeError("未安装 Pillow，请先安装 pillow Python 包")

    base = ImageOps.exif_transpose(img).convert("RGB")
    width, height = base.size
    if width <= 0 or height <= 0:
        return []

    lanczos = _resample_filter("LANCZOS")
    bicubic = _resample_filter("BICUBIC")
    variants: list[tuple[str, Image.Image, float, float]] = []

    def add(name: str, image: Image.Image, scale_x: float = 1.0, scale_y: float = 1.0) -> None:
        variants.append((name, image, scale_x, scale_y))

    gray = ImageOps.autocontrast(ImageOps.grayscale(base))
    denoised = gray.filter(ImageFilter.MedianFilter(size=3)).filter(ImageFilter.GaussianBlur(radius=0.35))
    small_size = (max(1, width // 2), max(1, height // 2))
    demoire = (
        gray.resize(small_size, lanczos)
        .resize((width, height), lanczos)
        .filter(ImageFilter.SHARPEN)
    )

    add("original", base.copy())
    add("gray_autocontrast", gray)
    add("denoise_blur", denoised)
    add("demoire_downup", demoire)

    for name, source in (
        ("gray_up2", gray),
        ("denoise_up2", denoised),
        ("demoire_up2", demoire),
    ):
        add(name, source.resize((width * 2, height * 2), bicubic).filter(ImageFilter.SHARPEN), 2.0, 2.0)

    threshold = (
        gray.resize((width * 2, height * 2), bicubic)
        .filter(ImageFilter.GaussianBlur(radius=0.3))
        .point(lambda p: 255 if p >= 165 else 0)
    )
    add("threshold_up2", threshold, 2.0, 2.0)
    return variants


def extract_ocr_tokens(
    img: Image.Image,
    lang: str,
    config: str = "",
    scale_x: float = 1.0,
    scale_y: float = 1.0,
) -> list[OcrToken]:
    """Run OCR with coordinates and return non-empty word tokens."""
    if pytesseract is None:
        raise RuntimeError("未安装 pytesseract，请先安装 pytesseract Python 包和 tesseract OCR 程序")
    data = pytesseract.image_to_data(img, lang=lang, config=config, output_type=pytesseract.Output.DICT)
    tokens: list[OcrToken] = []
    for index, raw_text in enumerate(data.get("text", [])):
        text = (raw_text or "").strip()
        normalized = normalize_text(text)
        if not normalized:
            continue

        left = round(_int_at(data, "left", index) / scale_x)
        top = round(_int_at(data, "top", index) / scale_y)
        width = round(_int_at(data, "width", index) / scale_x)
        height = round(_int_at(data, "height", index) / scale_y)
        if width <= 0 or height <= 0:
            continue

        line_key = (
            _int_at(data, "page_num", index),
            _int_at(data, "block_num", index),
            _int_at(data, "par_num", index),
            _int_at(data, "line_num", index),
        )
        tokens.append(OcrToken(text, normalized, (left, top, left + width, top + height), line_key))
    return tokens


def extract_ocr_tokens_multi(
    img: Image.Image,
    lang: str,
    psm_modes: tuple[int, ...] = DEFAULT_PSM_MODES,
) -> list[OcrToken]:
    """Run OCR over multiple preprocessed variants and page segmentation modes."""
    tokens: list[OcrToken] = []
    errors: list[str] = []
    variants = build_ocr_variants(img)
    for _variant_name, variant, scale_x, scale_y in variants:
        for psm in psm_modes:
            config = f"--oem 3 --psm {psm}"
            try:
                tokens.extend(extract_ocr_tokens(variant, lang, config=config, scale_x=scale_x, scale_y=scale_y))
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
    if not tokens and errors:
        raise RuntimeError(errors[-1])
    return tokens


def _find_all(text: str, needle: str) -> Iterable[int]:
    start = text.find(needle)
    while start != -1:
        yield start
        start = text.find(needle, start + 1)


def find_sensitive_boxes(
    tokens: list[OcrToken],
    sensitive_words: dict[str, str],
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    min_fuzzy_length: int = DEFAULT_MIN_FUZZY_LENGTH,
) -> tuple[list[tuple[int, int, int, int]], list[str]]:
    """Find OCR boxes that contain sensitive words, including words split across OCR tokens."""
    boxes: list[tuple[int, int, int, int]] = []
    triggered: set[str] = set()
    seen_boxes: set[tuple[int, int, int, int]] = set()

    def add_box(box: tuple[int, int, int, int]) -> None:
        if box not in seen_boxes:
            seen_boxes.add(box)
            boxes.append(box)

    # Direct match: the full sensitive value is inside one OCR token.
    for token in tokens:
        for normalized, original in sensitive_words.items():
            if normalized in token.normalized:
                add_box(token.box)
                triggered.add(original)

    # Line match: the sensitive value may have been split into several OCR tokens.
    lines: dict[tuple[int, int, int, int], list[OcrToken]] = {}
    for token in tokens:
        lines.setdefault(token.line_key, []).append(token)

    for line_tokens in lines.values():
        line_text_parts: list[str] = []
        char_to_token: list[int] = []
        for token_index, token in enumerate(line_tokens):
            line_text_parts.append(token.normalized)
            char_to_token.extend([token_index] * len(token.normalized))
        line_text = "".join(line_text_parts)
        if not line_text:
            continue

        for normalized, original in sensitive_words.items():
            for start in _find_all(line_text, normalized):
                end = start + len(normalized)
                token_indexes = set(char_to_token[start:end])
                for token_index in token_indexes:
                    add_box(line_tokens[token_index].box)
                triggered.add(original)

            if fuzzy_threshold <= 0 or len(normalized) < min_fuzzy_length:
                continue
            min_window = max(min_fuzzy_length, len(normalized) - 2)
            max_window = min(len(line_text), len(normalized) + 2)
            for window_size in range(min_window, max_window + 1):
                if window_size > len(line_text):
                    continue
                for start in range(0, len(line_text) - window_size + 1):
                    end = start + window_size
                    candidate = line_text[start:end]
                    if not candidate[0].isalnum() or not candidate[-1].isalnum():
                        continue
                    if fuzzy_similarity(candidate, normalized) < fuzzy_threshold:
                        continue
                    token_indexes = set(char_to_token[start:end])
                    for token_index in token_indexes:
                        add_box(line_tokens[token_index].box)
                    triggered.add(original)

    return boxes, sorted(triggered)


def _expand_box(
    box: tuple[int, int, int, int],
    padding: int,
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    width, height = image_size
    left, top, right, bottom = box
    return (
        max(0, left - padding),
        max(0, top - padding),
        min(width, right + padding),
        min(height, bottom + padding),
    )


def redact_image_in_place(
    image_path: str | Path,
    boxes: list[tuple[int, int, int, int]],
    padding: int,
    dry_run: bool = False,
) -> bool:
    """Draw black rectangles over boxes and replace the original image."""
    if not boxes or dry_run:
        return False
    if Image is None or ImageDraw is None:
        raise RuntimeError("未安装 Pillow，请先安装 pillow Python 包")

    path = Path(image_path)
    original_stat = path.stat()
    with Image.open(path) as original:
        image_format = original.format
        image = original.convert("RGB")
        draw = ImageDraw.Draw(image)
        for box in boxes:
            draw.rectangle(_expand_box(box, padding, image.size), fill=(0, 0, 0))

        suffix = path.suffix or ".png"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=suffix, dir=str(path.parent))
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            save_kwargs = {}
            if (image_format or "").upper() in {"JPEG", "JPG"}:
                save_kwargs.update({"quality": 95, "subsampling": 0})
            if image_format:
                image.save(tmp_path, format=image_format, **save_kwargs)
            else:
                image.save(tmp_path, **save_kwargs)
            tmp_path.chmod(original_stat.st_mode & 0o777)
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
    return True


def scan_single_image(
    image_path: str | Path,
    sensitive_words: dict[str, str],
    lang: str = DEFAULT_OCR_LANG,
    padding: int = 4,
    dry_run: bool = False,
    psm_modes: tuple[int, ...] = DEFAULT_PSM_MODES,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    min_fuzzy_length: int = DEFAULT_MIN_FUZZY_LENGTH,
) -> ScanResult:
    """扫描单张图片；命中时用黑块原地替换敏感区域。"""
    try:
        if Image is None:
            raise RuntimeError("未安装 Pillow，请先安装 pillow Python 包")
        with Image.open(image_path) as img:
            tokens = extract_ocr_tokens_multi(img, lang=lang, psm_modes=psm_modes)
        boxes, triggered_words = find_sensitive_boxes(
            tokens,
            sensitive_words,
            fuzzy_threshold=fuzzy_threshold,
            min_fuzzy_length=min_fuzzy_length,
        )
        redacted = redact_image_in_place(image_path, boxes, padding=padding, dry_run=dry_run)
        return ScanResult(triggered_words=triggered_words, boxes=boxes, redacted=redacted)
    except Exception as exc:  # noqa: BLE001
        print(f"无法解析图片 {image_path}: {exc}")
        return ScanResult(triggered_words=[], boxes=[], redacted=False)


def scan_directory(
    dir_path: str | Path,
    txt_path: str | Path,
    lang: str = DEFAULT_OCR_LANG,
    padding: int = 4,
    dry_run: bool = False,
    psm_modes: tuple[int, ...] = DEFAULT_PSM_MODES,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    min_fuzzy_length: int = DEFAULT_MIN_FUZZY_LENGTH,
) -> None:
    """遍历目录并扫描所有图片；命中后默认直接原地脱敏。"""
    root_dir = Path(dir_path)
    if not root_dir.is_dir():
        print(f"错误：指定的路径不是一个有效的目录 -> {root_dir}")
        return

    print("正在加载敏感词库...")
    sensitive_words = load_sensitive_words(txt_path)
    if not sensitive_words:
        print("词库为空，或未找到词库文件，退出扫描。")
        return

    print(f"开始扫描目录: {root_dir}")
    print(f"处理模式: {'仅检测，不修改图片' if dry_run else '命中后原地打黑块替换图片'}")
    print("-" * 50)

    total_scanned = 0
    total_alerts = 0
    total_redacted = 0

    for current_root, _dirs, files in os.walk(root_dir):
        for file in files:
            if not file.lower().endswith(SUPPORTED_EXTENSIONS):
                continue

            full_path = Path(current_root) / file
            total_scanned += 1
            print(f"[{total_scanned}] 正在扫描: {file} ...", end="\r")

            result = scan_single_image(
                full_path,
                sensitive_words,
                lang=lang,
                padding=padding,
                dry_run=dry_run,
                psm_modes=psm_modes,
                fuzzy_threshold=fuzzy_threshold,
                min_fuzzy_length=min_fuzzy_length,
            )

            if result.triggered_words:
                total_alerts += 1
                if result.redacted:
                    total_redacted += 1
                print(" " * 100, end="\r")
                print("【高危报警】")
                print(f"风险图片: {full_path}")
                print(f"命中敏感词: {result.triggered_words}")
                print(f"覆盖区域数: {len(result.boxes)}")
                print(f"处理结果: {'已原地脱敏替换' if result.redacted else '已检测，未修改图片'}")
                print("-" * 50)

    print("\n扫描结束！")
    print(
        f"统计报告：共检查图片 {total_scanned} 张，发现风险图片 {total_alerts} 张，"
        f"已脱敏替换 {total_redacted} 张。"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="批量扫描并脱敏目录下包含敏感账号/密码信息的图片")
    parser.add_argument("-d", "--dir", required=True, help="要扫描的图片目录路径，例如 ./grading-1/figures")
    parser.add_argument("-a", "--accounts", required=True, help="包含敏感词的文本文件路径，例如 accounts.txt")
    parser.add_argument("--lang", default=DEFAULT_OCR_LANG, help=f"Tesseract OCR 语言，默认 {DEFAULT_OCR_LANG}")
    parser.add_argument("--psm", default=",".join(str(v) for v in DEFAULT_PSM_MODES), help="逗号分隔的 Tesseract PSM 模式，默认 6,11")
    parser.add_argument("--padding", type=int, default=4, help="黑块相对 OCR 文字框额外扩展像素，默认 4")
    parser.add_argument("--fuzzy-threshold", type=float, default=DEFAULT_FUZZY_THRESHOLD, help="长敏感词模糊匹配阈值，0 表示关闭，默认 0.9")
    parser.add_argument("--min-fuzzy-length", type=int, default=DEFAULT_MIN_FUZZY_LENGTH, help="启用模糊匹配的最短敏感词长度，默认 6")
    parser.add_argument("--dry-run", action="store_true", help="只报告命中位置，不修改原图")

    args = parser.parse_args()
    psm_modes = tuple(int(value.strip()) for value in args.psm.split(",") if value.strip())
    scan_directory(
        args.dir,
        args.accounts,
        lang=args.lang,
        padding=args.padding,
        dry_run=args.dry_run,
        psm_modes=psm_modes or DEFAULT_PSM_MODES,
        fuzzy_threshold=args.fuzzy_threshold,
        min_fuzzy_length=args.min_fuzzy_length,
    )
