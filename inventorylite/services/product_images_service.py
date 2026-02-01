from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import json

from inventorylite import db
from inventorylite.utils import get_data_dir


@dataclass
class PendingImage:
    temp_path: Path
    original_name: str
    rel_path: str
    sort_order: int
    is_primary: bool
    filename: str


@dataclass
class ProductImage:
    id: int
    product_id: int
    rel_path: str
    original_name: str
    sort_order: int
    is_primary: bool
    created_at: str | None


class ProductImagesService:
    def __init__(self, data_dir: Path | None = None) -> None:
        # Image storage layout (relative to data dir):
        # images/products/<product_id>/<filename>
        self.data_dir = data_dir or get_data_dir()

    def list_images(self, product_id: int) -> list[ProductImage]:
        rows = db.list_product_images(product_id)
        return [
            ProductImage(
                id=int(row["id"]),
                product_id=int(row["product_id"]),
                rel_path=row["rel_path"],
                original_name=row["original_name"] or "",
                sort_order=int(row["sort_order"]),
                is_primary=bool(row["is_primary"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def stage_add_images(
        self,
        product_id: int | None,
        file_paths: Iterable[str],
        session_id: str,
        *,
        starting_sort: int = 0,
        primary_if_empty: bool = False,
    ) -> list[PendingImage]:
        staged: list[PendingImage] = []
        dest_dir = self._pending_dir(session_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        meta = self._read_pending_meta(dest_dir)
        sort_order = starting_sort
        for path_str in file_paths:
            source = Path(path_str)
            if not source.is_file():
                continue
            suffix = source.suffix.lower()
            filename = self._unique_filename(suffix)
            rel_path = self._build_rel_path(product_id, filename)
            dest_path = dest_dir / filename
            shutil.copy2(source, dest_path)
            is_primary = primary_if_empty and sort_order == starting_sort
            meta.append(
                {
                    "filename": filename,
                    "original_name": source.name,
                    "sort_order": sort_order,
                    "is_primary": is_primary,
                }
            )
            staged.append(
                PendingImage(
                    temp_path=dest_path,
                    original_name=source.name,
                    rel_path=rel_path,
                    sort_order=sort_order,
                    is_primary=is_primary,
                    filename=filename,
                )
            )
            sort_order += 1
        self._write_pending_meta(dest_dir, meta)
        return staged

    def apply_pending(self, session_id: str, product_id: int) -> list[ProductImage]:
        pending_dir = self._pending_dir(session_id)
        if not pending_dir.exists():
            return []
        final_dir = self._product_dir(product_id)
        final_dir.mkdir(parents=True, exist_ok=True)
        pending_items = self._load_pending_items(pending_dir, product_id)
        created: list[ProductImage] = []
        for item in pending_items:
            target = final_dir / Path(item.rel_path).name
            shutil.move(str(item.temp_path), target)
            img_id = db.add_product_image(
                product_id=product_id,
                rel_path=item.rel_path,
                original_name=item.original_name,
                sort_order=item.sort_order,
                is_primary=False,
            )
            created.append(
                ProductImage(
                    id=img_id,
                    product_id=product_id,
                    rel_path=item.rel_path,
                    original_name=item.original_name,
                    sort_order=item.sort_order,
                    is_primary=False,
                    created_at=None,
                )
            )
        self.cleanup_pending(session_id)
        return created

    def cleanup_pending(self, session_id: str) -> None:
        pending_dir = self._pending_dir(session_id)
        if pending_dir.exists():
            shutil.rmtree(pending_dir, ignore_errors=True)

    def list_pending(self, session_id: str) -> list[PendingImage]:
        pending_dir = self._pending_dir(session_id)
        if not pending_dir.exists():
            return []
        return self._load_pending_items(pending_dir, None)

    def delete_pending_image(self, session_id: str, filename: str) -> None:
        pending_dir = self._pending_dir(session_id)
        meta = self._read_pending_meta(pending_dir)
        meta = [item for item in meta if item.get("filename") != filename]
        target = pending_dir / filename
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        self._write_pending_meta(pending_dir, meta)

    def delete_image(self, image_id: int) -> None:
        row = db.get_product_image(image_id)
        if not row:
            return
        abs_path = self._abs_from_rel(row["rel_path"])
        db.delete_product_image(image_id)
        if abs_path.exists():
            try:
                abs_path.unlink()
            except OSError:
                pass

    def set_primary(self, image_id: int) -> None:
        db.set_primary_product_image(image_id)

    def delete_product_files(self, product_id: int) -> None:
        folder = self._product_dir(product_id)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)

    def abs_path(self, rel_path: str) -> Path:
        return self._abs_from_rel(rel_path)

    def _pending_dir(self, session_id: str) -> Path:
        return self.data_dir / "tmp" / "product_images" / session_id

    def _product_dir(self, product_id: int) -> Path:
        return self.data_dir / "images" / "products" / str(product_id)

    def _build_rel_path(self, product_id: int | None, filename: str) -> str:
        product_part = str(product_id) if product_id is not None else "pending"
        return f"images/products/{product_part}/{filename}"

    def _unique_filename(self, suffix: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]
        suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
        return f"{stamp}_{token}{suffix}"

    def _abs_from_rel(self, rel_path: str) -> Path:
        rel = rel_path.strip().lstrip("/\\")
        return self.data_dir / Path(rel)

    def _load_pending_items(self, pending_dir: Path, product_id: int | None) -> list[PendingImage]:
        meta = self._read_pending_meta(pending_dir)
        items: list[PendingImage] = []
        for item in sorted(meta, key=lambda x: int(x.get("sort_order", 0))):
            filename = item.get("filename")
            if not filename:
                continue
            temp_path = pending_dir / filename
            rel_path = self._build_rel_path(product_id, filename)
            items.append(
                PendingImage(
                    temp_path=temp_path,
                    original_name=item.get("original_name") or filename,
                    rel_path=rel_path,
                    sort_order=int(item.get("sort_order", 0)),
                    is_primary=bool(item.get("is_primary")),
                    filename=filename,
                )
            )
        return items

    def _pending_meta_path(self, pending_dir: Path) -> Path:
        return pending_dir / "pending.json"

    def _read_pending_meta(self, pending_dir: Path) -> list[dict]:
        meta_path = self._pending_meta_path(pending_dir)
        if not meta_path.exists():
            return []
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

    def _write_pending_meta(self, pending_dir: Path, meta: list[dict]) -> None:
        meta_path = self._pending_meta_path(pending_dir)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
