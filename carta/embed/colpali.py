"""ColPali/ColQwen2 multimodal embedding for carta.

Provides late-interaction visual retrieval by embedding PDF pages as
multi-vector patch representations (1024 patches × 128 dimensions).

This module uses the native transformers implementation of ColPali/ColQwen2,
which was added in transformers>=4.49.  The older colpali-engine package is
no longer required and has been dropped because its PEFT-based model loading
is incompatible with transformers>=5.x (KeyError: 'llava' in
_MOE_TARGET_MODULE_MAPPING / AttributeError: use_bidirectional_attention).

Supported checkpoints (HF-native, no PEFT adapters):
    ColPali  → vidore/colpali-v1.3-hf
    ColQwen2 → vidore/colqwen2-v1.0-hf   (default, lower VRAM)
    ColQwen2.5 → vidore/colqwen2.5-v0.2-hf
"""

import io
import sys
from pathlib import Path
from typing import Optional, Tuple, Union

try:
    import numpy as np
    from PIL import Image
except ImportError:
    np = None  # type: ignore
    Image = None  # type: ignore

# Native transformers ColPali/ColQwen2 — added in transformers>=4.49
_COLPALI_AVAILABLE = False
try:
    import torch
    from transformers import (
        ColPaliForRetrieval,
        ColPaliProcessor,
        ColQwen2ForRetrieval,
        ColQwen2Processor,
    )
    _COLPALI_AVAILABLE = True
except ImportError:
    torch = None  # type: ignore
    ColPaliForRetrieval = None  # type: ignore
    ColPaliProcessor = None  # type: ignore
    ColQwen2ForRetrieval = None  # type: ignore
    ColQwen2Processor = None  # type: ignore

try:
    import fitz  # PyMuPDF — for PDF page rendering
except ImportError:
    fitz = None  # type: ignore


# Default render DPI for page images
DEFAULT_RENDER_DPI = 150

# ColPali produces 128-dimensional patch vectors
# Number of patches depends on image size (typically 1024 for standard pages)
VECTOR_DIM = 128


class ColPaliError(Exception):
    """Raised when ColPali operations fail."""
    pass


class ColPaliEmbedder:
    """Embedder for ColPali/ColQwen2 late-interaction visual retrieval.

    Uses the native transformers ColPali/ColQwen2 implementation
    (ColPaliForRetrieval / ColQwen2ForRetrieval) — no colpali-engine required.

    Lazily loads the model on first embed call. Supports CPU, CUDA, and MPS.
    Caches loaded model to avoid reloads across multiple PDFs.

    Args:
        model_name: HuggingFace model name.
                    ColPali:   "vidore/colpali-v1.3-hf"
                    ColQwen2:  "vidore/colqwen2-v1.0-hf"  (default)
        device: Device to run inference on ("cpu", "cuda", "mps")
        batch_size: Number of pages to process per batch
        cache_dir: Directory to store cached page PNGs

    Example:
        >>> embedder = ColPaliEmbedder(
        ...     model_name="vidore/colqwen2-v1.0-hf",
        ...     device="cuda",
        ...     cache_dir=Path(".carta/visual_cache/")
        ... )
        >>> vectors, png_bytes = embedder.embed_pdf_page(Path("doc.pdf"), page_num=1)
    """

    _MODEL_CACHE: dict = {}  # Class-level cache: model_name -> (model, processor)

    def __init__(
        self,
        model_name: str = "vidore/colqwen2-v1.0-hf",
        device: str = "cpu",
        batch_size: int = 1,
        cache_dir: Optional[Union[str, Path]] = None,
    ):
        if not _COLPALI_AVAILABLE:
            raise ImportError(
                "transformers>=4.49 with ColPali/ColQwen2 support is required. "
                "Install with: pip install 'carta-cc[visual]'"
            )
        if np is None or Image is None:
            raise ImportError(
                "numpy and pillow are required for ColPaliEmbedder. "
                "Install with: pip install 'carta-cc[visual]'"
            )

        self.model_name = model_name
        self.device = self._resolve_device(device)
        self.batch_size = batch_size
        self.cache_dir = Path(cache_dir) if cache_dir else Path(".carta/visual_cache/")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._model = None
        self._processor = None
        # Detect architecture from model name
        self._is_colqwen = "qwen" in model_name.lower()

    def _resolve_device(self, device: str) -> str:
        """Resolve device string to available device.

        Falls back to CPU if requested device is unavailable.

        Args:
            device: Requested device ("cpu", "cuda", "mps")

        Returns:
            Available device string.
        """
        if device == "cuda" and torch is not None:
            if not torch.cuda.is_available():
                print(
                    "Warning: CUDA requested but not available, falling back to CPU",
                    file=sys.stderr,
                    flush=True,
                )
                return "cpu"
        elif device == "mps" and torch is not None:
            if not torch.backends.mps.is_available():
                print(
                    "Warning: MPS requested but not available, falling back to CPU",
                    file=sys.stderr,
                    flush=True,
                )
                return "cpu"
        return device

    def _load_model(self) -> Tuple:
        """Load model and processor using native transformers, with class-level cache.

        Returns:
            Tuple of (model, processor).
        """
        if self.model_name in self._MODEL_CACHE:
            self._model, self._processor = self._MODEL_CACHE[self.model_name]
            return self._model, self._processor

        print(f"Loading ColPali model: {self.model_name}...", flush=True)

        dtype = torch.bfloat16 if self.device in ("cuda", "mps") else torch.float32

        if self._is_colqwen:
            model = ColQwen2ForRetrieval.from_pretrained(
                self.model_name,
                torch_dtype=dtype,
                device_map=self.device,
            ).eval()
            processor = ColQwen2Processor.from_pretrained(self.model_name)
        else:
            model = ColPaliForRetrieval.from_pretrained(
                self.model_name,
                torch_dtype=dtype,
                device_map=self.device,
            ).eval()
            processor = ColPaliProcessor.from_pretrained(self.model_name)

        # Cache for reuse
        self._MODEL_CACHE[self.model_name] = (model, processor)
        self._model = model
        self._processor = processor

        print(f"Model loaded on {self.device}", flush=True)
        return model, processor

    def embed_page(self, image: "Image.Image") -> "np.ndarray":
        """Embed a single page image as multi-vector patches.

        Args:
            image: PIL Image of the page.

        Returns:
            numpy array of shape (num_patches, 128) containing patch vectors.

        Raises:
            ColPaliError: If embedding fails.
        """
        if self._model is None:
            self._load_model()

        try:
            # Native transformers API: processor(images=[...]) → forward → .embeddings
            inputs = self._processor(images=[image], return_tensors="pt").to(self.device)

            with torch.no_grad():
                outputs = self._model(**inputs)
                # Native ColPali/ColQwen2 returns ColPaliForRetrievalOutput
                # with .embeddings of shape (batch, num_patches, hidden_dim)
                embeddings = outputs.embeddings

            # Convert to numpy (num_patches, 128)
            vectors = embeddings.cpu().float().numpy()
            return vectors[0]  # Return first (and only) item in batch

        except Exception as exc:
            raise ColPaliError(f"Failed to embed page: {exc}") from exc

    def embed_pdf_page(
        self,
        pdf_path: Path,
        page_num: int,
        dpi: int = DEFAULT_RENDER_DPI,
    ) -> Tuple["np.ndarray", bytes]:
        """Render and embed a single PDF page.

        Args:
            pdf_path: Path to the PDF file.
            page_num: 1-indexed page number to embed.
            dpi: Resolution for page rendering.

        Returns:
            Tuple of (vectors array, PNG bytes).

        Raises:
            ColPaliError: If PDF cannot be opened or page doesn't exist.
            ImportError: If PyMuPDF is not available.
        """
        if fitz is None:
            raise ImportError("PyMuPDF (fitz) is required for PDF page rendering")

        try:
            doc = fitz.open(str(pdf_path))
        except Exception as exc:
            raise ColPaliError(f"Cannot open PDF {pdf_path}: {exc}") from exc

        try:
            if page_num < 1 or page_num > len(doc):
                raise ColPaliError(
                    f"Page {page_num} out of range (PDF has {len(doc)} pages)"
                )

            page = doc[page_num - 1]  # PyMuPDF uses 0-indexing
            pix = page.get_pixmap(dpi=dpi)
            png_bytes = pix.tobytes("png")

            # Convert to PIL Image for embedding
            image = Image.open(io.BytesIO(png_bytes))
            if image.mode != "RGB":
                image = image.convert("RGB")

            # Embed the page
            vectors = self.embed_page(image)

            return vectors, png_bytes

        finally:
            doc.close()

    def embed_pdf_pages(
        self,
        pdf_path: Path,
        page_nums: Optional[list[int]] = None,
        dpi: int = DEFAULT_RENDER_DPI,
    ) -> list[dict]:
        """Embed multiple PDF pages in batches.

        Args:
            pdf_path: Path to the PDF file.
            page_nums: List of 1-indexed page numbers. If None, embeds all pages.
            dpi: Resolution for page rendering.

        Returns:
            List of dicts with keys: page_num, vectors, png_bytes.

        Raises:
            ColPaliError: If PDF cannot be opened.
        """
        if fitz is None:
            raise ImportError("PyMuPDF (fitz) is required for PDF page rendering")

        # Ensure model is loaded before processing
        if self._model is None:
            self._load_model()

        try:
            doc = fitz.open(str(pdf_path))
        except Exception as exc:
            raise ColPaliError(f"Cannot open PDF {pdf_path}: {exc}") from exc

        try:
            if page_nums is None:
                page_nums = list(range(1, len(doc) + 1))

            results = []

            # Process in batches
            for i in range(0, len(page_nums), self.batch_size):
                batch = page_nums[i : i + self.batch_size]
                batch_images = []
                batch_meta = []

                for page_num in batch:
                    if page_num < 1 or page_num > len(doc):
                        print(
                            f"Warning: Page {page_num} out of range, skipping",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue

                    page = doc[page_num - 1]
                    pix = page.get_pixmap(dpi=dpi)
                    png_bytes = pix.tobytes("png")
                    image = Image.open(io.BytesIO(png_bytes))
                    if image.mode != "RGB":
                        image = image.convert("RGB")

                    batch_images.append(image)
                    batch_meta.append({"page_num": page_num, "png_bytes": png_bytes})

                if not batch_images:
                    continue

                # Embed batch using native transformers API
                try:
                    inputs = self._processor(
                        images=batch_images, return_tensors="pt"
                    ).to(self.device)

                    with torch.no_grad():
                        outputs = self._model(**inputs)
                        embeddings = outputs.embeddings

                    # Convert to numpy
                    vectors_batch = embeddings.cpu().float().numpy()

                    # Store results
                    for idx, meta in enumerate(batch_meta):
                        results.append({
                            "page_num": meta["page_num"],
                            "vectors": vectors_batch[idx],
                            "png_bytes": meta["png_bytes"],
                        })

                except Exception as exc:
                    print(
                        f"Warning: Failed to embed batch starting at page {batch[0]}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue

            return results

        finally:
            doc.close()

    def save_page_cache(
        self,
        pdf_path: Path,
        page_num: int,
        png_bytes: bytes,
    ) -> Path:
        """Save page PNG to the visual cache directory.

        Args:
            pdf_path: Path to the source PDF.
            page_num: 1-indexed page number.
            png_bytes: PNG image bytes.

        Returns:
            Path to the saved PNG file.
        """
        # Create subdirectory for this PDF
        pdf_cache_dir = self.cache_dir / pdf_path.stem
        pdf_cache_dir.mkdir(parents=True, exist_ok=True)

        png_path = pdf_cache_dir / f"page_{page_num:04d}.png"
        png_path.write_bytes(png_bytes)

        return png_path

    def embed_query(self, query: str) -> "np.ndarray":
        """Embed a text query as multi-vector patches for late-interaction retrieval.

        Encodes the query text using the native transformers ColPali/ColQwen2
        processor for MaxSim scoring against document patch vectors.

        Args:
            query: Natural language search query string.

        Returns:
            numpy array of shape (num_query_tokens, 128) containing query vectors.

        Raises:
            ColPaliError: If query encoding fails.
        """
        if self._model is None:
            self._load_model()

        try:
            # Native transformers API: processor(text=[...]) for query encoding
            inputs = self._processor(text=[query], return_tensors="pt").to(self.device)

            with torch.no_grad():
                outputs = self._model(**inputs)
                embeddings = outputs.embeddings

            # Convert to numpy (num_tokens, 128)
            vectors = embeddings.cpu().float().numpy()
            return vectors[0]  # Return first (and only) item in batch

        except Exception as exc:
            raise ColPaliError(f"Failed to encode query: {exc}") from exc


def is_colpali_available() -> bool:
    """Check if ColPali dependencies are available.

    Returns:
        True if transformers>=4.49 with ColPali support, torch, and numpy are installed.
    """
    return _COLPALI_AVAILABLE and np is not None
