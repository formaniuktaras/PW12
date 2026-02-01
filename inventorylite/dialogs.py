from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
from typing import Optional
import uuid

from PIL import Image, ImageTk

from inventorylite import db, sku_gen
from inventorylite.helpers import _find_index_by_name, _sanitize_barcode_prefix
from inventorylite.services.product_images_service import ProductImagesService
from inventorylite.utils import Settings

def product_prompt(
    brands,
    categories,
    title: str,
    initial=None,
    settings: Settings | None = None,
    *,
    product_id: int | None = None,
):
    base_initial = {
        "sku": "",
        "supplier_sku": "",
        "name": "",
        "brand_id": None,
        "category_id": None,
        "unit": (settings.get("defaults", "product", "unit") if settings else None) or "pcs",
        "is_active": True,
        "extras": [],
        "supplier_codes": [],
        "barcodes": [],
    }

    if isinstance(initial, dict):
        normalized_initial = {**base_initial, **initial}
    elif initial:
        # Support tuples/lists passed by the callers
        normalized_initial = base_initial.copy()
        normalized_initial.update(
            {
                "sku": initial[0] if len(initial) > 0 else "",
                "supplier_sku": initial[1] if len(initial) > 1 else "",
                "name": initial[2] if len(initial) > 2 else "",
                "brand_id": initial[3] if len(initial) > 3 else None,
                "category_id": initial[4] if len(initial) > 4 else None,
                "unit": initial[5] if len(initial) > 5 else "pcs",
                "is_active": bool(initial[6]) if len(initial) > 6 else True,
                "extras": initial[7] if len(initial) > 7 else [],
                "supplier_codes": initial[8] if len(initial) > 8 else [],
                "barcodes": initial[9] if len(initial) > 9 else [],
            }
        )
    else:
        normalized_initial = base_initial

    dlg = tk.Toplevel()
    dlg.title(title)
    dlg.grab_set()
    dlg.columnconfigure(1, weight=1)
    dlg.columnconfigure(2, weight=0)

    images_service = ProductImagesService()
    session_id = uuid.uuid4().hex
    deleted_image_ids: set[int] = set()
    pending_images: list[dict] = []
    existing_images: list[dict] = []
    selected_index: int | None = None
    primary_ref: dict | None = None

    def _open_file(path: Path) -> None:
        if not path.exists():
            messagebox.showerror("Фото", "Файл не знайдено.")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            if os.name == "nt":
                return
            cmd = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.run([cmd, str(path)], check=False)

    def _palette() -> dict[str, str]:
        style = ttk.Style()
        return {
            "bg": style.lookup("TFrame", "background") or "#2b2b2b",
            "surface": style.lookup("TLabel", "background") or "#2b2b2b",
            "surface_alt": style.lookup("TEntry", "fieldbackground") or "#3a3a3a",
            "text": style.lookup("TLabel", "foreground") or "#e6e6e6",
            "muted": "#9aa0a6",
            "border": "#4a4a4a",
            "accent": "#3b82f6",
        }

    palette = _palette()

    photo_frame = ttk.Frame(dlg)
    photo_frame.grid(row=0, column=2, rowspan=11, padx=(12, 6), pady=6, sticky="nsew")
    photo_frame.columnconfigure(0, weight=1)
    photo_frame.rowconfigure(1, weight=1)

    ttk.Label(photo_frame, text="Фото").grid(row=0, column=0, sticky="w", padx=4, pady=(0, 6))

    preview_container = tk.Frame(photo_frame, background=palette["surface_alt"], highlightthickness=1, width=360, height=240)
    preview_container.configure(highlightbackground=palette["border"], highlightcolor=palette["border"])
    preview_container.grid(row=1, column=0, sticky="nsew", padx=4)
    preview_container.grid_propagate(False)
    preview_container.columnconfigure(0, weight=1)
    preview_container.rowconfigure(0, weight=1)

    preview_label = tk.Label(
        preview_container,
        text="Немає фото",
        background=palette["surface_alt"],
        foreground=palette["muted"],
        anchor="center",
    )
    preview_label.grid(row=0, column=0, sticky="nsew")

    thumb_canvas = tk.Canvas(
        photo_frame,
        height=110,
        background=palette["bg"],
        highlightthickness=0,
        bd=0,
    )
    thumb_scroll = ttk.Scrollbar(photo_frame, orient="horizontal", command=thumb_canvas.xview)
    thumb_canvas.configure(xscrollcommand=thumb_scroll.set)
    thumb_canvas.grid(row=2, column=0, sticky="ew", padx=4, pady=(6, 2))
    thumb_scroll.grid(row=3, column=0, sticky="ew", padx=4)

    thumbs_inner = tk.Frame(thumb_canvas, background=palette["bg"])
    thumb_canvas.create_window((0, 0), window=thumbs_inner, anchor="nw")

    action_row = ttk.Frame(photo_frame)
    action_row.grid(row=4, column=0, sticky="ew", padx=4, pady=(6, 0))

    preview_photo: ImageTk.PhotoImage | None = None
    thumbnail_cache: dict[str, ImageTk.PhotoImage] = {}

    def _combined_images() -> list[dict]:
        combined = [img for img in existing_images if img["id"] not in deleted_image_ids]
        combined.extend(pending_images)
        return combined

    def _sync_primary_ref() -> None:
        nonlocal primary_ref
        if primary_ref and primary_ref.get("kind") == "existing":
            if primary_ref.get("id") in deleted_image_ids:
                primary_ref = None

        combined = _combined_images()
        if not combined:
            primary_ref = None
            return
        if primary_ref:
            return
        primary_ref = {"kind": combined[0]["kind"]}
        if combined[0]["kind"] == "existing":
            primary_ref["id"] = combined[0]["id"]
        else:
            primary_ref["filename"] = combined[0]["filename"]

    def _current_image() -> dict | None:
        combined = _combined_images()
        if selected_index is None or not combined:
            return None
        if selected_index < 0 or selected_index >= len(combined):
            return None
        return combined[selected_index]

    def _apply_primary_flags() -> None:
        _sync_primary_ref()
        for img in existing_images:
            img["is_primary"] = False
        for img in pending_images:
            img["is_primary"] = False
        if not primary_ref:
            return
        if primary_ref.get("kind") == "existing":
            for img in existing_images:
                if img["id"] == primary_ref.get("id"):
                    img["is_primary"] = True
        elif primary_ref.get("kind") == "pending":
            for img in pending_images:
                if img["filename"] == primary_ref.get("filename"):
                    img["is_primary"] = True

    def _render_preview(img: dict | None) -> None:
        nonlocal preview_photo
        if not img or not img.get("path") or not Path(img["path"]).exists():
            preview_label.configure(text="Немає фото", image="", compound="none")
            preview_photo = None
            return
        preview_label.configure(text="")
        target_w = max(preview_label.winfo_width(), 1)
        target_h = max(preview_label.winfo_height(), 1)
        try:
            with Image.open(img["path"]) as source:
                source.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
                preview_photo = ImageTk.PhotoImage(source)
                preview_label.configure(image=preview_photo)
        except Exception:
            preview_label.configure(text="Немає фото", image="", compound="none")
            preview_photo = None

    def _thumbnail_for(path: Path, size: int = 96) -> ImageTk.PhotoImage | None:
        key = f"{path}:{size}"
        if key in thumbnail_cache:
            return thumbnail_cache[key]
        try:
            with Image.open(path) as image:
                image.thumbnail((size - 8, size - 8), Image.Resampling.LANCZOS)
                thumb = ImageTk.PhotoImage(image)
                thumbnail_cache[key] = thumb
                return thumb
        except Exception:
            return None

    def _refresh_thumbnails() -> None:
        nonlocal selected_index
        for child in thumbs_inner.winfo_children():
            child.destroy()
        combined = _combined_images()
        if selected_index is None and combined:
            selected_index = 0
        if combined and selected_index is not None:
            selected_index = max(0, min(selected_index, len(combined) - 1))
        _sync_primary_ref()
        _apply_primary_flags()
        for idx, img in enumerate(combined):
            frame = tk.Frame(thumbs_inner, background=palette["bg"])
            frame.grid(row=0, column=idx, padx=4, pady=4)
            canvas = tk.Canvas(
                frame,
                width=96,
                height=96,
                background=palette["surface_alt"],
                highlightthickness=2,
                highlightbackground=palette["accent"] if idx == selected_index else palette["border"],
                highlightcolor=palette["accent"],
                bd=0,
            )
            canvas.pack()
            thumb = _thumbnail_for(Path(img["path"]))
            if thumb:
                canvas.create_image(48, 48, image=thumb)
                canvas.image = thumb
            if img.get("is_primary"):
                canvas.create_rectangle(62, 72, 92, 92, fill=palette["accent"], outline=palette["accent"])
                canvas.create_text(77, 82, text="★", fill="#ffffff", font=("TkDefaultFont", 9, "bold"))

            def _on_select(_event=None, index=idx):
                nonlocal selected_index
                selected_index = index
                _refresh_thumbnails()
                _render_preview(_current_image())

            def _on_open(_event=None, index=idx):
                selected = combined[index]
                _open_file(Path(selected["path"]))

            canvas.bind("<Button-1>", _on_select)
            canvas.bind("<Double-Button-1>", _on_open)
            canvas.bind("<Enter>", lambda _e, c=canvas: c.configure(highlightbackground=palette["accent"]))
            canvas.bind("<Leave>", lambda _e, c=canvas, i=idx: c.configure(
                highlightbackground=palette["accent"] if i == selected_index else palette["border"]
            ))

        thumbs_inner.update_idletasks()
        thumb_canvas.configure(scrollregion=thumb_canvas.bbox("all"))
        _render_preview(_current_image())

    def _set_primary() -> None:
        nonlocal primary_ref
        current = _current_image()
        if not current:
            return
        if current["kind"] == "existing":
            primary_ref = {"kind": "existing", "id": current["id"]}
        else:
            primary_ref = {"kind": "pending", "filename": current["filename"]}
        _refresh_thumbnails()

    def _delete_selected() -> None:
        nonlocal selected_index
        current = _current_image()
        if not current:
            return
        if messagebox.askyesno("Фото", "Видалити вибране фото?"):
            if current["kind"] == "existing":
                deleted_image_ids.add(current["id"])
            else:
                images_service.delete_pending_image(session_id, current["filename"])
                pending_images[:] = [img for img in pending_images if img["filename"] != current["filename"]]
            selected_index = None
            _sync_primary_ref()
            _refresh_thumbnails()

    def _add_images() -> None:
        nonlocal selected_index
        file_paths = filedialog.askopenfilenames(
            title="Додати фото",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp"), ("All files", "*.*")],
            initialdir=str(images_service.data_dir),
        )
        if not file_paths:
            return
        valid_paths: list[str] = []
        for path in file_paths:
            try:
                with Image.open(path) as img:
                    img.verify()
            except Exception:
                messagebox.showerror("Фото", f"Не вдалося відкрити файл: {Path(path).name}")
                continue
            valid_paths.append(path)
        if not valid_paths:
            return
        combined = _combined_images()
        max_sort = max((img.get("sort_order", 0) for img in combined), default=-1)
        starting_sort = max_sort + 1
        primary_if_empty = not combined
        staged = images_service.stage_add_images(
            product_id,
            valid_paths,
            session_id,
            starting_sort=starting_sort,
            primary_if_empty=primary_if_empty,
        )
        for item in staged:
            pending_images.append(
                {
                    "kind": "pending",
                    "filename": item.filename,
                    "path": item.temp_path,
                    "sort_order": item.sort_order,
                    "is_primary": item.is_primary,
                }
            )
        if primary_if_empty and staged:
            primary_ref = {"kind": "pending", "filename": staged[0].filename}
        if selected_index is None and staged:
            selected_index = len(combined)
        _refresh_thumbnails()

    def _on_preview_resize(_event=None):
        _render_preview(_current_image())

    def _open_selected_preview(_event=None) -> None:
        current = _current_image()
        if not current:
            return
        _open_file(Path(current["path"]))

    preview_label.bind("<Double-Button-1>", _open_selected_preview)
    preview_label.bind("<Configure>", _on_preview_resize)

    ttk.Button(action_row, text="＋ Додати", command=_add_images).pack(side=tk.LEFT, padx=4)
    ttk.Button(action_row, text="Видалити", command=_delete_selected, style="Danger.TButton").pack(
        side=tk.LEFT, padx=4
    )
    ttk.Button(action_row, text="Зробити основним", command=_set_primary).pack(side=tk.LEFT, padx=4)

    def _handle_key(event: tk.Event) -> None:
        nonlocal selected_index
        combined = _combined_images()
        if not combined:
            return
        if event.keysym in ("Left", "Right"):
            if selected_index is None:
                selected_index = 0
            else:
                delta = -1 if event.keysym == "Left" else 1
                selected_index = max(0, min(selected_index + delta, len(combined) - 1))
            _refresh_thumbnails()
        elif event.keysym == "Delete":
            _delete_selected()

    dlg.bind("<Left>", _handle_key)
    dlg.bind("<Right>", _handle_key)
    dlg.bind("<Delete>", _handle_key)

    if product_id:
        for row in images_service.list_images(product_id):
            abs_path = images_service.abs_path(row.rel_path)
            existing_images.append(
                {
                    "kind": "existing",
                    "id": row.id,
                    "path": abs_path,
                    "sort_order": row.sort_order,
                    "is_primary": row.is_primary,
                }
            )
        primary_row = next((img for img in existing_images if img["is_primary"]), None)
        if primary_row:
            primary_ref = {"kind": "existing", "id": primary_row["id"]}

    for item in images_service.list_pending(session_id):
        pending_images.append(
            {
                "kind": "pending",
                "filename": item.filename,
                "path": item.temp_path,
                "sort_order": item.sort_order,
                "is_primary": item.is_primary,
            }
        )

    _refresh_thumbnails()

    ttk.Label(dlg, text="Артикул (SKU)").grid(row=0, column=0, padx=6, pady=4, sticky="w")
    sku_var = tk.StringVar(value=normalized_initial.get("sku", ""))
    sku_frame = ttk.Frame(dlg)
    sku_frame.grid(row=0, column=1, padx=6, pady=4, sticky="ew")
    sku_frame.columnconfigure(0, weight=1)
    ttk.Entry(sku_frame, textvariable=sku_var, width=30).grid(row=0, column=0, padx=(0, 4), pady=0, sticky="ew")
    generate_btn = ttk.Button(sku_frame, text="Згенерувати")
    generate_btn.grid(row=0, column=1, padx=0, pady=0)

    ttk.Label(dlg, text="Артикул постачальника (legacy)").grid(row=1, column=0, padx=6, pady=4, sticky="w")
    supplier_sku_var = tk.StringVar(value=normalized_initial.get("supplier_sku", ""))
    ttk.Entry(dlg, textvariable=supplier_sku_var, width=30).grid(row=1, column=1, padx=6, pady=4, sticky="ew")

    ttk.Label(dlg, text="Назва").grid(row=2, column=0, padx=6, pady=4, sticky="w")
    name_var = tk.StringVar(value=normalized_initial.get("name", ""))
    ttk.Entry(dlg, textvariable=name_var, width=30).grid(row=2, column=1, padx=6, pady=4, sticky="ew")

    ttk.Label(dlg, text="Бренд").grid(row=3, column=0, padx=6, pady=4, sticky="w")
    brand_var = tk.StringVar()
    brand_names = [b["name"] for b in brands]
    brand_combo = ttk.Combobox(dlg, textvariable=brand_var, state="readonly", values=brand_names)
    brand_combo.grid(row=3, column=1, padx=6, pady=4, sticky="ew")

    ttk.Label(dlg, text="Головна категорія").grid(row=4, column=0, padx=6, pady=4, sticky="w")
    category_var = tk.StringVar()
    category_combo = ttk.Combobox(dlg, textvariable=category_var, values=[c["label"] for c in categories])
    category_combo.grid(row=4, column=1, padx=6, pady=4, sticky="ew")

    generator_enabled = bool(settings.get("defaults", "product", "sku_generator", "enabled") if settings else False)

    def _selected_brand():
        try:
            idx = brand_combo.current()
            if idx is None or idx < 0:
                return None
            return brands[idx]
        except Exception:
            return None

    def _selected_category():
        label = category_var.get()
        return next((c for c in categories if c.get("label") == label), None)

    def _generate_sku() -> None:
        if not generator_enabled or not settings:
            return
        brand_row = _selected_brand()
        category_row = _selected_category()
        try:
            sku_value = sku_gen.generate_next_sku(settings, brand_row, category_row, name_var.get())
        except Exception as exc:
            messagebox.showerror("SKU", f"Не вдалося згенерувати SKU: {exc}")
            return
        sku_var.set(sku_value)

    generate_btn.configure(command=_generate_sku, state="normal" if generator_enabled else "disabled")

    def refresh_category_options(*_args):
        search = category_var.get().strip().lower()
        filtered = [c["label"] for c in categories if search in c["label"].lower()]
        category_combo["values"] = filtered or [c["label"] for c in categories]

    category_combo.bind("<KeyRelease>", refresh_category_options)

    ttk.Label(dlg, text="Додаткові категорії").grid(row=5, column=0, padx=6, pady=4, sticky="nw")
    extras_frame = ttk.Frame(dlg)
    extras_frame.grid(row=5, column=1, padx=6, pady=4, sticky="nsew")
    extras_frame.columnconfigure(0, weight=1)
    extras_frame.rowconfigure(1, weight=1)
    extras_search_var = tk.StringVar()
    ttk.Entry(extras_frame, textvariable=extras_search_var).grid(
        row=0, column=0, columnspan=2, padx=(0, 8), pady=(0, 4), sticky="ew"
    )
    extras_box = tk.Listbox(
        extras_frame, selectmode=tk.MULTIPLE, height=min(10, max(6, len(categories))), exportselection=False
    )
    extras_scroll = ttk.Scrollbar(extras_frame, orient="vertical", command=extras_box.yview)
    extras_box.configure(yscrollcommand=extras_scroll.set)
    extras_box.grid(row=1, column=0, sticky="nsew")
    extras_scroll.grid(row=1, column=1, sticky="ns")

    filtered_extra_categories = list(categories)
    initial_extra_ids = set(normalized_initial.get("extras") or [])

    def refresh_extra_list(*_args):
        selected_labels = {extras_box.get(i) for i in extras_box.curselection()}
        search = extras_search_var.get().strip().lower()
        extras_box.delete(0, tk.END)
        filtered_extra_categories.clear()
        filtered_extra_categories.extend([c for c in categories if search in c["label"].lower()])
        for idx, cat in enumerate(filtered_extra_categories):
            extras_box.insert(tk.END, cat["label"])
            if cat["label"] in selected_labels or cat["id"] in initial_extra_ids:
                extras_box.selection_set(idx)
        initial_extra_ids.difference_update({c["id"] for c in filtered_extra_categories})

    extras_search_var.trace_add("write", refresh_extra_list)
    refresh_extra_list()

    dlg.rowconfigure(5, weight=1)

    ttk.Label(dlg, text="Одиниця").grid(row=6, column=0, padx=6, pady=4, sticky="w")
    unit_var = tk.StringVar(value=normalized_initial.get("unit", "pcs"))
    ttk.Entry(dlg, textvariable=unit_var, width=12).grid(row=6, column=1, padx=6, pady=4, sticky="w")

    is_active_var = tk.BooleanVar(value=normalized_initial.get("is_active", True))
    ttk.Checkbutton(dlg, text="Активний", variable=is_active_var).grid(row=7, column=1, padx=6, pady=4, sticky="w")

    suppliers = db.list_suppliers()
    supplier_names = [s["name"] for s in suppliers]
    supplier_name_by_id = {s["id"]: s["name"] for s in suppliers}
    supplier_codes_state: list[dict] = []
    for code in normalized_initial.get("supplier_codes") or []:
        try:
            supplier_codes_state.append(
                {
                    "supplier_id": int(code.get("supplier_id")),  # type: ignore[arg-type]
                    "supplier_sku": (code.get("supplier_sku") or "").strip(),
                    "is_primary": bool(code.get("is_primary")),
                }
            )
        except (TypeError, ValueError):
            continue

    barcode_prefix = _sanitize_barcode_prefix((settings.get("defaults", "product", "barcode_prefix") if settings else "") or "")
    barcodes_state: list[dict] = []
    for code in normalized_initial.get("barcodes") or []:
        raw = (code.get("code") or "").strip()
        if not raw:
            continue
        barcodes_state.append({"code": raw, "note": (code.get("note") or "").strip()})

    supplier_codes_frame = ttk.LabelFrame(dlg, text="Артикули постачальників")
    supplier_codes_frame.grid(row=8, column=0, columnspan=2, padx=6, pady=4, sticky="nsew")
    supplier_codes_frame.columnconfigure(1, weight=1)
    dlg.rowconfigure(8, weight=1)

    ttk.Label(supplier_codes_frame, text="Постачальник:").grid(row=0, column=0, padx=4, pady=2, sticky="w")
    supplier_var = tk.StringVar()
    supplier_combo = ttk.Combobox(
        supplier_codes_frame, textvariable=supplier_var, state="readonly", values=supplier_names, width=30
    )
    supplier_combo.grid(row=0, column=1, padx=4, pady=2, sticky="ew")

    ttk.Label(supplier_codes_frame, text="Артикул:").grid(row=1, column=0, padx=4, pady=2, sticky="w")
    supplier_code_var = tk.StringVar()
    ttk.Entry(supplier_codes_frame, textvariable=supplier_code_var, width=30).grid(
        row=1, column=1, padx=4, pady=2, sticky="ew"
    )

    supplier_primary_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(supplier_codes_frame, text="Основний", variable=supplier_primary_var).grid(
        row=1, column=2, padx=4, pady=2, sticky="w"
    )

    def refresh_supplier_codes_tree() -> None:
        supplier_codes_tree.delete(*supplier_codes_tree.get_children())
        for idx, code in enumerate(supplier_codes_state):
            supplier_name = supplier_name_by_id.get(code.get("supplier_id"), str(code.get("supplier_id")))
            supplier_codes_tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(supplier_name, code.get("supplier_sku", ""), "Так" if code.get("is_primary") else ""),
            )

    def add_supplier_code() -> None:
        if not supplier_names:
            messagebox.showerror("Артикули постачальників", "Створіть постачальника зі статусом постачальника.")
            return
        try:
            supplier_idx = supplier_names.index(supplier_var.get())
        except ValueError:
            messagebox.showerror("Артикули постачальників", "Оберіть постачальника")
            return
        supplier_id = suppliers[supplier_idx]["id"]
        supplier_sku = supplier_code_var.get().strip()
        if not supplier_sku:
            messagebox.showerror("Артикули постачальників", "Введіть артикул постачальника")
            return
        key = (supplier_id, supplier_sku.lower())
        if any((c.get("supplier_id"), (c.get("supplier_sku") or "").lower()) == key for c in supplier_codes_state):
            messagebox.showerror("Артикули постачальників", "Такий артикул вже додано для цього постачальника")
            return
        is_primary = bool(supplier_primary_var.get())
        if is_primary:
            for c in supplier_codes_state:
                if c.get("supplier_id") == supplier_id:
                    c["is_primary"] = False
        supplier_codes_state.append(
            {"supplier_id": supplier_id, "supplier_sku": supplier_sku, "is_primary": is_primary}
        )
        refresh_supplier_codes_tree()
        supplier_code_var.set("")
        supplier_primary_var.set(False)

    def delete_supplier_code() -> None:
        selection = supplier_codes_tree.selection()
        if not selection:
            return
        idx = int(selection[0])
        if 0 <= idx < len(supplier_codes_state):
            supplier_codes_state.pop(idx)
        refresh_supplier_codes_tree()

    def mark_primary() -> None:
        selection = supplier_codes_tree.selection()
        if not selection:
            return
        idx = int(selection[0])
        if 0 <= idx < len(supplier_codes_state):
            supplier_id = supplier_codes_state[idx].get("supplier_id")
            for i, code in enumerate(supplier_codes_state):
                if code.get("supplier_id") == supplier_id:
                    code["is_primary"] = i == idx
        refresh_supplier_codes_tree()

    ttk.Button(supplier_codes_frame, text="Додати", command=add_supplier_code).grid(
        row=0, column=2, padx=4, pady=2, sticky="w"
    )

    columns = ("supplier", "sku", "primary")
    supplier_codes_tree = ttk.Treeview(
        supplier_codes_frame, columns=columns, show="headings", selectmode="browse", height=6
    )
    supplier_codes_tree.heading("supplier", text="Постачальник")
    supplier_codes_tree.heading("sku", text="Артикул")
    supplier_codes_tree.heading("primary", text="Основний")
    supplier_codes_tree.column("supplier", width=180, anchor="w")
    supplier_codes_tree.column("sku", width=120, anchor="w")
    supplier_codes_tree.column("primary", width=90, anchor="center")
    supplier_codes_tree.grid(row=2, column=0, columnspan=3, padx=4, pady=4, sticky="nsew")
    supplier_codes_frame.rowconfigure(2, weight=1)
    supplier_codes_frame.columnconfigure(0, weight=1)
    supplier_codes_frame.columnconfigure(1, weight=1)
    scroll = ttk.Scrollbar(supplier_codes_frame, orient="vertical", command=supplier_codes_tree.yview)
    supplier_codes_tree.configure(yscrollcommand=scroll.set)
    scroll.grid(row=2, column=3, sticky="ns")

    actions_frame = ttk.Frame(supplier_codes_frame)
    actions_frame.grid(row=3, column=0, columnspan=3, padx=4, pady=(0, 4), sticky="w")
    ttk.Button(actions_frame, text="Видалити вибраний", command=delete_supplier_code).pack(side=tk.LEFT, padx=4)
    ttk.Button(actions_frame, text="Зробити основним", command=mark_primary).pack(side=tk.LEFT, padx=4)

    if supplier_names:
        supplier_combo.current(0)
    refresh_supplier_codes_tree()

    barcodes_frame = ttk.LabelFrame(dlg, text="Додаткові штрихкоди")
    barcodes_frame.grid(row=9, column=0, columnspan=2, padx=6, pady=4, sticky="nsew")
    barcodes_frame.columnconfigure(1, weight=1)
    dlg.rowconfigure(9, weight=1)

    def _main_barcode_value() -> str:
        return f"{barcode_prefix}{sku_var.get().strip()}"

    main_barcode_label = ttk.Label(barcodes_frame, text=f"Основний (SKU): {_main_barcode_value()}")
    main_barcode_label.grid(row=0, column=0, columnspan=3, padx=4, pady=2, sticky="w")

    def _update_main_barcode(*_args) -> None:
        main_barcode_label.configure(text=f"Основний (SKU): {_main_barcode_value()}")

    sku_var.trace_add("write", _update_main_barcode)

    ttk.Label(barcodes_frame, text="Штрихкод:").grid(row=1, column=0, padx=4, pady=2, sticky="w")
    barcode_code_var = tk.StringVar()
    ttk.Entry(barcodes_frame, textvariable=barcode_code_var, width=22).grid(row=1, column=1, padx=4, pady=2, sticky="ew")

    ttk.Label(barcodes_frame, text="Нотатка:").grid(row=2, column=0, padx=4, pady=2, sticky="w")
    barcode_note_var = tk.StringVar()
    ttk.Entry(barcodes_frame, textvariable=barcode_note_var, width=22).grid(row=2, column=1, padx=4, pady=2, sticky="ew")

    def refresh_barcode_tree() -> None:
        barcode_tree.delete(*barcode_tree.get_children())
        for idx, code in enumerate(barcodes_state):
            barcode_tree.insert("", "end", iid=str(idx), values=(code.get("code", ""), code.get("note", "")))

    def add_barcode() -> None:
        code_val = barcode_code_var.get().strip()
        if not code_val:
            messagebox.showerror("Штрихкоди", "Введіть штрихкод")
            return
        normalized = code_val.lower()
        if any((c.get("code") or "").lower() == normalized for c in barcodes_state):
            messagebox.showerror("Штрихкоди", "Такий штрихкод вже додано")
            return
        barcodes_state.append({"code": code_val, "note": barcode_note_var.get().strip()})
        refresh_barcode_tree()
        barcode_code_var.set("")
        barcode_note_var.set("")

    def delete_barcode() -> None:
        selection = barcode_tree.selection()
        if not selection:
            return
        idx = int(selection[0])
        if 0 <= idx < len(barcodes_state):
            barcodes_state.pop(idx)
        refresh_barcode_tree()

    ttk.Button(barcodes_frame, text="Додати", command=add_barcode).grid(row=1, column=2, padx=4, pady=2, sticky="w")

    barcode_tree = ttk.Treeview(barcodes_frame, columns=("code", "note"), show="headings", selectmode="browse", height=5)
    barcode_tree.heading("code", text="Штрихкод")
    barcode_tree.heading("note", text="Нотатка")
    barcode_tree.column("code", width=180, anchor="w")
    barcode_tree.column("note", width=180, anchor="w")
    barcode_tree.grid(row=3, column=0, columnspan=3, padx=4, pady=4, sticky="nsew")
    barcodes_frame.rowconfigure(3, weight=1)
    barcodes_frame.columnconfigure(0, weight=1)
    barcodes_frame.columnconfigure(1, weight=1)
    barcode_scroll = ttk.Scrollbar(barcodes_frame, orient="vertical", command=barcode_tree.yview)
    barcode_tree.configure(yscrollcommand=barcode_scroll.set)
    barcode_scroll.grid(row=3, column=3, sticky="ns")

    ttk.Button(barcodes_frame, text="Видалити вибраний", command=delete_barcode).grid(
        row=4, column=0, columnspan=3, padx=4, pady=(0, 4), sticky="w"
    )

    refresh_barcode_tree()

    if initial:
        brand_combo.current(next((i for i, b in enumerate(brands) if b["id"] == normalized_initial["brand_id"]), 0))
        category_var.set(next((c["label"] for c in categories if c["id"] == normalized_initial["category_id"]), ""))
        refresh_category_options()
        extras = set(normalized_initial.get("extras") or [])
        for idx, cat in enumerate(filtered_extra_categories):
            if cat["id"] in extras:
                extras_box.selection_set(idx)
    else:
        preferred_brand = _find_index_by_name(brand_names, (settings.get("defaults", "product", "brand") if settings else ""))
        if preferred_brand is not None:
            brand_combo.current(preferred_brand)
        elif brands:
            brand_combo.current(0)
        if categories:
            default_category = (settings.get("defaults", "product", "category") if settings else "") or ""
            if default_category:
                category_var.set(default_category)
            else:
                category_var.set(categories[0]["label"])
            refresh_category_options()

    result = None

    def on_ok():
        nonlocal result
        sku = sku_var.get().strip()
        name = name_var.get().strip()
        if not sku and generator_enabled and settings:
            _generate_sku()
            sku = sku_var.get().strip()
        if not sku or not name:
            messagebox.showerror("Валідація", "Заповніть SKU та назву")
            return
        if not categories:
            messagebox.showerror("Валідація", "Створіть принаймні одну категорію")
            return
        try:
            brand_id = brands[brand_combo.current()]["id"]
        except IndexError:
            messagebox.showerror("Валідація", "Оберіть бренд і категорію")
            return
        selected_label = category_var.get().strip()
        matched_category = next((c for c in categories if c["label"].lower() == selected_label.lower()), None)
        if not matched_category:
            matched_category = next((c for c in categories if selected_label.lower() in c["label"].lower()), None)
        if not matched_category:
            messagebox.showerror("Валідація", "Оберіть бренд і категорію")
            return
        category_id = matched_category["id"]
        unit = unit_var.get().strip()
        is_active = bool(is_active_var.get())

        supplier_codes_payload: list[dict] = []
        for code in supplier_codes_state:
            try:
                supplier_id = int(code.get("supplier_id"))
            except (TypeError, ValueError):
                continue
            sku_val = (code.get("supplier_sku") or "").strip()
            if not sku_val:
                continue
            supplier_codes_payload.append(
                {
                    "supplier_id": supplier_id,
                    "supplier_sku": sku_val,
                    "is_primary": bool(code.get("is_primary")),
                }
            )

        barcodes_payload: list[dict] = []
        for code in barcodes_state:
            value = (code.get("code") or "").strip()
            if not value:
                continue
            barcodes_payload.append({"code": value, "note": (code.get("note") or "").strip()})

        extras_ids = [filtered_extra_categories[i]["id"] for i in extras_box.curselection()]
        _sync_primary_ref()
        image_payload = {
            "session_id": session_id,
            "deleted_ids": sorted(deleted_image_ids),
            "primary": primary_ref.copy() if primary_ref else None,
        }
        result = (
            sku,
            supplier_sku_var.get().strip(),
            name,
            brand_id,
            category_id,
            unit,
            is_active,
            extras_ids,
            supplier_codes_payload,
            barcodes_payload,
            image_payload,
        )
        dlg.destroy()

    def on_cancel():
        images_service.cleanup_pending(session_id)
        dlg.destroy()

    btns = ttk.Frame(dlg)
    btns.grid(row=10, column=0, columnspan=2, pady=8)
    ttk.Button(btns, text="OK", command=on_ok).pack(side=tk.LEFT, padx=4)
    ttk.Button(btns, text="Скасувати", command=on_cancel).pack(side=tk.LEFT, padx=4)
    dlg.bind("<Return>", lambda e: on_ok())
    dlg.bind("<Escape>", lambda e: on_cancel())
    dlg.protocol("WM_DELETE_WINDOW", on_cancel)
    dlg.wait_window()
    return result


def category_prompt(title: str, initial=None):
    dlg = tk.Toplevel()
    dlg.title(title)
    dlg.grab_set()

    ttk.Label(dlg, text="Назва").grid(row=0, column=0, padx=6, pady=4, sticky="w")
    name_var = tk.StringVar(value=initial[0] if initial else "")
    ttk.Entry(dlg, textvariable=name_var, width=30).grid(row=0, column=1, padx=6, pady=4)

    ttk.Label(dlg, text="Колір (hex)").grid(row=1, column=0, padx=6, pady=4, sticky="w")
    color_var = tk.StringVar(value=initial[1] if initial else "")
    ttk.Entry(dlg, textvariable=color_var, width=20).grid(row=1, column=1, padx=6, pady=4, sticky="w")

    ttk.Label(dlg, text="Іконка/emoji").grid(row=2, column=0, padx=6, pady=4, sticky="w")
    icon_var = tk.StringVar(value=initial[2] if initial else "")
    ttk.Entry(dlg, textvariable=icon_var, width=20).grid(row=2, column=1, padx=6, pady=4, sticky="w")

    ttk.Label(dlg, text="Типові атрибути (через кому)").grid(row=3, column=0, padx=6, pady=4, sticky="w")
    attrs_var = tk.StringVar(value=initial[3] if initial else "")
    ttk.Entry(dlg, textvariable=attrs_var, width=40).grid(row=3, column=1, padx=6, pady=4, sticky="w")

    service_var = tk.BooleanVar(value=initial[4] if initial else False)
    hidden_var = tk.BooleanVar(value=initial[5] if initial else False)
    ttk.Checkbutton(dlg, text="Службова", variable=service_var).grid(row=4, column=1, padx=6, pady=4, sticky="w")
    ttk.Checkbutton(dlg, text="Прихована", variable=hidden_var).grid(row=5, column=1, padx=6, pady=4, sticky="w")

    result = None

    def on_ok():
        nonlocal result
        name = name_var.get().strip()
        if not name:
            messagebox.showerror("Валідація", "Назва обов'язкова")
            return
        result = (name, color_var.get().strip(), icon_var.get().strip(), attrs_var.get().strip(), service_var.get(), hidden_var.get())
        dlg.destroy()

    ttk.Button(dlg, text="OK", command=on_ok).grid(row=6, column=0, padx=6, pady=8)
    ttk.Button(dlg, text="Скасувати", command=dlg.destroy).grid(row=6, column=1, padx=6, pady=8)
    dlg.bind("<Return>", lambda e: on_ok())
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    dlg.wait_window()
    return result


def select_category_dialog(title: str, options: list[tuple[Optional[int], str]]) -> Optional[int]:
    dlg = tk.Toplevel()
    dlg.title(title)
    dlg.grab_set()

    ttk.Label(dlg, text="Категорія").grid(row=0, column=0, padx=6, pady=4, sticky="w")
    values = [opt[1] for opt in options]
    combo_var = tk.StringVar()
    combo = ttk.Combobox(dlg, textvariable=combo_var, state="readonly", values=values)
    combo.grid(row=0, column=1, padx=6, pady=4)
    combo.current(0 if values else -1)

    result: Optional[int] = None

    def on_ok():
        nonlocal result
        if not values:
            result = None
        else:
            idx = combo.current()
            result = options[idx][0]
        dlg.destroy()

    ttk.Button(dlg, text="OK", command=on_ok).grid(row=1, column=0, padx=6, pady=8)
    ttk.Button(dlg, text="Скасувати", command=dlg.destroy).grid(row=1, column=1, padx=6, pady=8)
    dlg.bind("<Return>", lambda e: on_ok())
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    dlg.wait_window()
    return result


def counterparty_prompt(initial=None, default_type: str | None = None):
    dlg = tk.Toplevel()
    dlg.title("Контрагент")
    dlg.grab_set()

    labels = ["Назва", "Телефон", "Email", "Адреса", "Нотатка"]
    vars_ = [tk.StringVar(value=initial[i] if initial else "") for i in [0, 2, 3, 4, 5]]

    ttk.Label(dlg, text="Назва").grid(row=0, column=0, padx=6, pady=4, sticky="w")
    name_entry = ttk.Entry(dlg, textvariable=vars_[0], width=30)
    name_entry.grid(row=0, column=1, padx=6, pady=4)

    ttk.Label(dlg, text="Тип").grid(row=1, column=0, padx=6, pady=4, sticky="w")
    type_var = tk.StringVar()
    types = ["Постачальник", "Покупець", "Постачальник/Покупець", "Інший"]
    type_values = {"Постачальник": "supplier", "Покупець": "customer", "Постачальник/Покупець": "both", "Інший": "other"}
    type_combo = ttk.Combobox(dlg, textvariable=type_var, values=types, state="readonly")
    type_combo.grid(row=1, column=1, padx=6, pady=4)
    if initial:
        inv_map = {v: k for k, v in type_values.items()}
        type_combo.set(inv_map.get(initial[1], types[0]))
    elif default_type:
        inv_map = {v: k for k, v in type_values.items()}
        type_combo.set(inv_map.get(default_type, types[0]))
    else:
        type_combo.current(0)

    for i, label in enumerate(labels[1:], start=2):
        ttk.Label(dlg, text=label).grid(row=i, column=0, padx=6, pady=4, sticky="w")
        ttk.Entry(dlg, textvariable=vars_[i - 1], width=30).grid(row=i, column=1, padx=6, pady=4)

    result = None

    def on_ok():
        nonlocal result
        name = vars_[0].get().strip()
        if not name:
            messagebox.showerror("Валідація", "Заповніть назву")
            return
        ctype = type_values.get(type_var.get(), "supplier")
        phone, email, address, note = [v.get().strip() for v in vars_[1:]]
        result = (name, ctype, phone, email, address, note)
        dlg.destroy()

    def on_cancel():
        dlg.destroy()

    btns = ttk.Frame(dlg)
    btns.grid(row=6, column=0, columnspan=2, pady=8)
    ttk.Button(btns, text="OK", command=on_ok).pack(side=tk.LEFT, padx=4)
    ttk.Button(btns, text="Скасувати", command=on_cancel).pack(side=tk.LEFT, padx=4)
    dlg.bind("<Return>", lambda e: on_ok())
    dlg.bind("<Escape>", lambda e: on_cancel())
    dlg.after_idle(name_entry.focus_set)
    dlg.wait_window()
    return result


def warehouse_prompt(initial=None):
    dlg = tk.Toplevel()
    dlg.title("Склад")
    dlg.grab_set()

    name_var = tk.StringVar(value=initial[0] if initial else "")
    desc_var = tk.StringVar(value=initial[1] if initial else "")
    active_var = tk.BooleanVar(value=initial[2] if initial else True)

    ttk.Label(dlg, text="Назва").grid(row=0, column=0, padx=6, pady=4, sticky="w")
    name_entry = ttk.Entry(dlg, textvariable=name_var, width=30)
    name_entry.grid(row=0, column=1, padx=6, pady=4)
    ttk.Label(dlg, text="Опис").grid(row=1, column=0, padx=6, pady=4, sticky="w")
    ttk.Entry(dlg, textvariable=desc_var, width=40).grid(row=1, column=1, padx=6, pady=4)
    ttk.Checkbutton(dlg, text="Активний", variable=active_var).grid(row=2, column=1, padx=6, pady=4, sticky="w")

    result = None

    def on_ok():
        nonlocal result
        name = name_var.get().strip()
        if not name:
            messagebox.showerror("Валідація", "Заповніть назву")
            return
        result = (name, desc_var.get().strip(), bool(active_var.get()))
        dlg.destroy()

    def on_cancel():
        dlg.destroy()

    btns = ttk.Frame(dlg)
    btns.grid(row=3, column=0, columnspan=2, pady=8)
    ttk.Button(btns, text="OK", command=on_ok).pack(side=tk.LEFT, padx=4)
    ttk.Button(btns, text="Скасувати", command=on_cancel).pack(side=tk.LEFT, padx=4)
    dlg.bind("<Return>", lambda e: (on_ok(), "break"))
    dlg.bind("<Escape>", lambda e: on_cancel())
    dlg.after_idle(name_entry.focus_set)
    dlg.wait_window()
    return result


def channel_prompt(initial=None):
    dlg = tk.Toplevel()
    dlg.title("Канал продажу")
    dlg.grab_set()
    name_var = tk.StringVar(value=initial[0] if initial else "")
    active_var = tk.BooleanVar(value=initial[1] if initial else True)

    ttk.Label(dlg, text="Назва").grid(row=0, column=0, padx=6, pady=4, sticky="w")
    name_entry = ttk.Entry(dlg, textvariable=name_var, width=30)
    name_entry.grid(row=0, column=1, padx=6, pady=4)
    ttk.Checkbutton(dlg, text="Активний", variable=active_var).grid(row=1, column=1, padx=6, pady=4, sticky="w")

    result = None

    def on_ok():
        nonlocal result
        name = name_var.get().strip()
        if not name:
            messagebox.showerror("Валідація", "Заповніть назву")
            return
        result = (name, bool(active_var.get()))
        dlg.destroy()

    def on_cancel():
        dlg.destroy()

    btns = ttk.Frame(dlg)
    btns.grid(row=2, column=0, columnspan=2, pady=8)
    ttk.Button(btns, text="OK", command=on_ok).pack(side=tk.LEFT, padx=4)
    ttk.Button(btns, text="Скасувати", command=on_cancel).pack(side=tk.LEFT, padx=4)
    dlg.bind("<Return>", lambda e: (on_ok(), "break"))
    dlg.bind("<Escape>", lambda e: on_cancel())
    dlg.after_idle(name_entry.focus_set)
    dlg.wait_window()
    return result
