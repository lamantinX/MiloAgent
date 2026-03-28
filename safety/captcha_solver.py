"""Reddit CAPTCHA auto-solver using open-source OCR libraries.

Reddit's old-style CAPTCHA: distorted text image served at
  https://www.reddit.com/captcha/{iden}
When a POST fails with BAD_CAPTCHA, retry with captcha_iden + captcha_sol.

Solver priority chain:
  1. ddddocr  -- purpose-built deep-learning CAPTCHA OCR (best accuracy)
  2. tesseract -- traditional OCR fallback (pytesseract + PIL pre-processing)
  3. None      -- give up, apply 2h cooldown as before

Install: pip install ddddocr pillow
Optional: apt install tesseract-ocr && pip install pytesseract
"""

import io
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CAPTCHA_URL = "https://www.reddit.com/captcha/{iden}"
FETCH_TIMEOUT = 10  # seconds


def _preprocess_image(img_bytes: bytes):
    """Pre-process CAPTCHA image for better OCR accuracy.

    Returns a PIL Image object, or None if PIL is not available.
    Steps: grayscale -> contrast boost -> threshold -> slight sharpen
    """
    try:
        from PIL import Image, ImageFilter, ImageEnhance
        import io as _io

        img = Image.open(_io.BytesIO(img_bytes)).convert("L")  # grayscale
        # Boost contrast
        img = ImageEnhance.Contrast(img).enhance(2.5)
        # Sharpen
        img = img.filter(ImageFilter.SHARPEN)
        # Resize 2x for better OCR
        w, h = img.size
        img = img.resize((w * 2, h * 2), Image.LANCZOS)
        return img
    except Exception as e:
        logger.debug(f"Image pre-processing failed: {e}")
        return None


def _solve_with_ddddocr(img_bytes: bytes) -> Optional[str]:
    """Solve CAPTCHA using ddddocr (best open-source CAPTCHA OCR)."""
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
        result = ocr.classification(img_bytes)
        result = result.strip().replace(" ", "")
        logger.info(f"ddddocr solved CAPTCHA: '{result}'")
        return result if result else None
    except ImportError:
        logger.debug("ddddocr not installed (pip install ddddocr)")
        return None
    except Exception as e:
        logger.debug(f"ddddocr failed: {e}")
        return None


def _solve_with_tesseract(img_bytes: bytes) -> Optional[str]:
    """Solve CAPTCHA using Tesseract OCR with image pre-processing."""
    try:
        import pytesseract
        img = _preprocess_image(img_bytes)
        if img is None:
            # Fallback: direct bytes if PIL not available
            try:
                from PIL import Image
                import io as _io
                img = Image.open(_io.BytesIO(img_bytes))
            except Exception:
                return None

        # Tesseract config: single-word, alphanumeric only
        config = "--psm 8 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        result = pytesseract.image_to_string(img, config=config)
        result = result.strip().replace(" ", "").replace("\n", "")
        logger.info(f"Tesseract solved CAPTCHA: '{result}'")
        return result if len(result) >= 3 else None
    except ImportError:
        logger.debug("pytesseract not installed (apt install tesseract-ocr && pip install pytesseract)")
        return None
    except Exception as e:
        logger.debug(f"Tesseract failed: {e}")
        return None


class RedditCaptchaSolver:
    """Fetches and solves Reddit CAPTCHA images.

    Usage:
        solver = RedditCaptchaSolver(session)
        solution = solver.solve(iden)
        if solution:
            # retry POST with captcha_iden=iden, captcha_sol=solution
    """

    def __init__(self, session: requests.Session):
        self.session = session
        self._attempts = 0
        self._solved = 0
        self._failed = 0

    def fetch_image(self, iden: str) -> Optional[bytes]:
        """Download CAPTCHA image bytes from Reddit."""
        url = CAPTCHA_URL.format(iden=iden)
        try:
            resp = self.session.get(url, timeout=FETCH_TIMEOUT)
            if resp.status_code == 200 and resp.content:
                logger.debug(f"Fetched CAPTCHA image: {len(resp.content)} bytes")
                return resp.content
            logger.warning(f"CAPTCHA fetch failed: HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"CAPTCHA fetch error: {e}")
        return None

    def solve(self, iden: str) -> Optional[str]:
        """Fetch and solve a Reddit CAPTCHA.

        Returns solved text string, or None if unsolvable.
        """
        self._attempts += 1
        img_bytes = self.fetch_image(iden)
        if not img_bytes:
            self._failed += 1
            return None

        # Try ddddocr first (best accuracy for distorted text)
        solution = _solve_with_ddddocr(img_bytes)
        if solution:
            self._solved += 1
            return solution

        # Fallback to Tesseract
        solution = _solve_with_tesseract(img_bytes)
        if solution:
            self._solved += 1
            return solution

        logger.warning(f"CAPTCHA unsolvable for iden={iden[:8]}... (no solver available)")
        self._failed += 1
        return None

    @property
    def solve_rate(self) -> float:
        """Return fraction of CAPTCHAs solved (0.0-1.0)."""
        if not self._attempts:
            return 0.0
        return self._solved / self._attempts

    def stats(self) -> dict:
        return {
            "attempts": self._attempts,
            "solved": self._solved,
            "failed": self._failed,
            "solve_rate": round(self.solve_rate, 2),
        }
