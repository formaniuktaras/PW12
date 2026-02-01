from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from typing import Optional, Callable

from inventorylite import db
from inventorylite import labels
from inventorylite import assembly_resolver
from inventorylite.label_templates_ui import TemplateManagerDialog
from inventorylite.ui_components import TableFrame, simple_prompt
from inventorylite.utils import (
    Settings,
    show_error,
    get_data_dir,
    backup_database,
    get_db_path,
    open_file,
)
from inventorylite.dialogs import product_prompt
from inventorylite.services.product_images_service import ProductImagesService
from inventorylite.dialogs_stock_moves import open_stock_moves_dialog
from inventorylite.helpers import (
    PRODUCT_FIELDS,
    _sanitize_barcode_prefix,
    parse_import_file,
    _suggest_product_mapping,
    _normalize_product_records,
)


class ProductsTab:
    def __init__(
        self,
        parent: ttk.Notebook,
        settings: Settings,
        flatten_categories_provider: Callable[[], list[dict]],
    ) -> None:
        self.parent = parent
        self.settings = settings
        self._flatten_categories_provider = flatten_categories_provider

        self.frame = ttk.Frame(parent)
        self.category_filter_options: list[dict] = []
        self._build()

    def _build(self) -> None:
        top = ttk.Frame(self.frame)
        top.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(top, text="Пошук:").pack(side=tk.LEFT)
        self.product_search_var = tk.StringVar()
        search_entry = ttk.Entry(top, textvariable=self.product_search_var, width=30)
        search_entry.pack(side=tk.LEFT, padx=4)
        search_entry.bind("<Return>", self.on_product_search_enter)
        ttk.Label(top, text="Категорія:").pack(side=tk.LEFT, padx=6)
        self.product_category_filter_var = tk.StringVar()
        self.product_category_filter_combo = ttk.Combobox(
            top, textvariable=self.product_category_filter_var, state="readonly", width=30
        )
        self.product_category_filter_combo.pack(side=tk.LEFT)
        self.include_subcategories_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Включно з підкатегоріями", variable=self.include_subcategories_var).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(top, text="Оновити", command=self.on_search_products).pack(side=tk.LEFT, padx=6)

        columns = [
            ("sku", "SKU", 120),
            ("barcode", "Штрихкод", 160),
            ("supplier_sku", "Артикул постачальника", 170),
            ("name", "Назва", 230),
            ("brand", "Бренд", 140),
            ("category", "Категорія", 140),
            ("extra_categories", "Додаткові категорії", 200),
            ("unit", "Одиниця", 90),
            ("is_active", "Активний", 90),
        ]
        self.product_table = TableFrame(
            self.frame,
            columns,
            selectmode="extended",
            settings=self.settings,
            persist_key="products_table",
        )
        self.product_table.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.product_table.on_double_click(self.edit_product)
        self.product_table.register_context_menu_actions(
            [
                ("Рух товару…", self.open_product_stock_moves),
                ("Редагувати", self.edit_product),
                ("Видалити", self.delete_product),
            ]
        )

        btns = ttk.Frame(self.frame)
        btns.pack(pady=4)
        ttk.Button(btns, text="Додати", command=self.add_product).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Змінити", command=self.edit_product).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Видалити", command=self.delete_product).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Імпорт із файлу", command=self.import_products_from_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            btns,
            text="Масові дії...",
            command=lambda: open_products_bulk_actions_dialog(self, db.get_connection(), self.product_table),
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Рух", command=self.open_product_stock_moves).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Комплектація", command=self.open_product_assemblies).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Коди каналів", command=self.open_product_channel_codes).pack(side=tk.LEFT, padx=4)

    def set_category_filter_options(self, options: list[dict]) -> None:
        self.category_filter_options = list(options or [])
        values = ["Усі категорії"] + [c["label"] for c in self.category_filter_options]
        self.product_category_filter_combo.configure(values=values)
        cur = self.product_category_filter_var.get()
        if not cur or cur not in values:
            self.product_category_filter_var.set("Усі категорії")
            try:
                self.product_category_filter_combo.current(0)
            except Exception:
                pass

    def flatten_categories(self) -> list[dict]:
        return self._flatten_categories_provider()

    def default_workdir(self) -> Path:
        path = self.settings.get("files", "working_dir") or str(get_data_dir())
        try:
            return Path(path)
        except Exception:
            return get_data_dir()

    def on_search_products(self) -> None:
        self.refresh_products(self.product_search_var.get())

    def on_product_search_enter(self, _event=None) -> None:
        value = self.product_search_var.get().strip()
        prefix = _sanitize_barcode_prefix(self.settings.get("defaults", "product", "barcode_prefix") or "")
        product = db.find_product_by_scan_code(value, prefix)
        if product:
            self.product_search_var.set("")
            self.product_category_filter_var.set("Усі категорії")
            self.refresh_products()
            pid = str(product["id"])
            self.product_table.tree.selection_set(pid)
            self.product_table.tree.focus(pid)
            self.product_table.tree.see(pid)
            return
        self.on_search_products()

    def refresh_products(self, search: str | None = None) -> None:
        category_id = None
        selected_label = self.product_category_filter_var.get()
        for option in getattr(self, "category_filter_options", []):
            if option["label"] == selected_label:
                category_id = option["id"]
                break

        rows = db.list_products(search, category_id, self.include_subcategories_var.get())
        prefix = _sanitize_barcode_prefix(self.settings.get("defaults", "product", "barcode_prefix") or "")
        self.product_table.set_rows(
            [
                {
                    "id": r["id"],
                    "sku": r["sku"],
                    "barcode": f"{prefix}{r['sku']}",
                    "supplier_sku": r["supplier_sku"] or "",
                    "name": r["name"],
                    "brand": r["brand"],
                    "category": r["category"],
                    "extra_categories": r["extra_categories"] or "",
                    "unit": r["unit"],
                    "is_active": "Так" if r["is_active"] else "Ні",
                }
                for r in rows
            ]
        )

    def add_product(self) -> None:
        brands = db.list_brands()
        categories = self.flatten_categories()
        values = product_prompt(brands, categories, "Новий товар", settings=self.settings)
        if not values:
            return
        (
            sku,
            supplier_sku_legacy,
            name,
            brand_id,
            category_id,
            unit,
            is_active,
            extras,
            supplier_codes,
            barcodes,
            image_payload,
        ) = values
        try:
            effective_supplier_sku = supplier_sku_legacy or (
                supplier_codes[0]["supplier_sku"] if len(supplier_codes) == 1 else None
            )
            product_id = db.add_product(sku, name, brand_id, category_id, unit, is_active, effective_supplier_sku)
            db.set_product_categories(product_id, category_id, extras)
            if supplier_codes:
                db.replace_product_supplier_codes(product_id, supplier_codes)
            if barcodes:
                db.replace_product_barcodes(product_id, barcodes)
            images_service = ProductImagesService()
            created = images_service.apply_pending(image_payload["session_id"], product_id)
            primary = image_payload.get("primary")
            if primary:
                if primary.get("kind") == "existing":
                    images_service.set_primary(int(primary.get("id")))
                elif primary.get("kind") == "pending":
                    filename = primary.get("filename")
                    match = next((img for img in created if Path(img.rel_path).name == filename), None)
                    if match:
                        images_service.set_primary(match.id)
            self.refresh_products()
        except sqlite3.IntegrityError as exc:
            ProductImagesService().cleanup_pending(image_payload["session_id"])
            if "ProductSupplierCodes" in str(exc):
                show_error(
                    "Товари",
                    "Артикул постачальника вже прив’язаний до іншого товару для цього постачальника.",
                )
            elif "ProductBarcodes" in str(exc) or "UNIQUE" in str(exc):
                show_error("Товари", "Цей штрихкод вже прив’язаний до іншого товару.")
            else:
                show_error("Товари", "SKU або назва вже існує.")
        except Exception:
            ProductImagesService().cleanup_pending(image_payload["session_id"])
            logging.exception("Add product error")
            show_error("Товари", "Не вдалося додати товар.")

    def edit_product(self) -> None:
        product_id = self.product_table.selected_id()
        if not product_id:
            show_error("Товари", "Оберіть товар для редагування.")
            return
        product = db.get_product(product_id)
        if not product:
            return
        brands = db.list_brands()
        categories = self.flatten_categories()
        supplier_codes = [
            {
                "supplier_id": row["supplier_id"],
                "supplier_sku": row["supplier_sku"],
                "is_primary": bool(row["is_primary"]),
            }
            for row in db.list_product_supplier_codes(product_id)
        ]
        barcodes = [
            {"code": row["code"], "note": row["note"] or ""}
            for row in db.list_product_barcodes(product_id)
        ]
        values = product_prompt(
            brands,
            categories,
            "Редагувати товар",
            (
                product["sku"],
                product.get("supplier_sku"),
                product["name"],
                product["brand_id"],
                product["category_id"],
                product["unit"],
                bool(product["is_active"]),
                db.get_product_additional_categories(product_id),
                supplier_codes,
                barcodes,
            ),
            settings=self.settings,
            product_id=product_id,
        )
        if not values:
            return
        (
            sku,
            supplier_sku_legacy,
            name,
            brand_id,
            category_id,
            unit,
            is_active,
            extras,
            supplier_codes,
            barcodes,
            image_payload,
        ) = values
        try:
            db.update_product(product_id, sku, name, brand_id, category_id, unit, is_active, supplier_sku_legacy)
            db.set_product_categories(product_id, category_id, extras)
            db.replace_product_supplier_codes(product_id, supplier_codes)
            db.replace_product_barcodes(product_id, barcodes)
            images_service = ProductImagesService()
            for image_id in image_payload.get("deleted_ids", []):
                images_service.delete_image(int(image_id))
            created = images_service.apply_pending(image_payload["session_id"], product_id)
            primary = image_payload.get("primary")
            if primary:
                if primary.get("kind") == "existing":
                    images_service.set_primary(int(primary.get("id")))
                elif primary.get("kind") == "pending":
                    filename = primary.get("filename")
                    match = next((img for img in created if Path(img.rel_path).name == filename), None)
                    if match:
                        images_service.set_primary(match.id)
            self.refresh_products()
        except sqlite3.IntegrityError as exc:
            ProductImagesService().cleanup_pending(image_payload["session_id"])
            if "ProductSupplierCodes" in str(exc):
                show_error(
                    "Товари",
                    "Артикул постачальника вже прив’язаний до іншого товару для цього постачальника.",
                )
            elif "ProductBarcodes" in str(exc) or "UNIQUE" in str(exc):
                show_error("Товари", "Цей штрихкод вже прив’язаний до іншого товару.")
            else:
                show_error("Товари", "SKU або назва вже існує.")
        except Exception:
            ProductImagesService().cleanup_pending(image_payload["session_id"])
            logging.exception("Edit product error")
            show_error("Товари", "Не вдалося змінити товар.")

    def delete_product(self) -> None:
        product_id = self.product_table.selected_id()
        if not product_id:
            show_error("Товари", "Оберіть товар для видалення.")
            return
        if not messagebox.askyesno("Підтвердження", "Видалити товар?"):
            return
        try:
            db.delete_product(product_id)
            ProductImagesService().delete_product_files(product_id)
            self.refresh_products()
        except Exception:
            logging.exception("Delete product error")
            show_error("Товари", "Не вдалося видалити товар.")

    def import_products_from_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Файл товарів",
            filetypes=[("CSV", "*.csv"), ("Excel", "*.xlsx *.xls"), ("Усі файли", "*.*")],
            initialdir=str(self.default_workdir()),
        )
        if not file_path:
            return

        try:
            raw_rows, headers = parse_import_file(Path(file_path), encoding=self.settings.get("files", "encoding") or "utf-8")
        except Exception as exc:
            logging.exception("Не вдалося прочитати файл імпорту товарів")
            show_error(
                "Імпорт товарів",
                "Не вдалося прочитати файл. Перевірте формат, кодування та структуру даних.\n" + str(exc),
            )
            return

        if not raw_rows:
            messagebox.showinfo("Імпорт товарів", "У файлі не знайдено рядків із товарами.")
            return

        dialog = ProductsImportDialog(self.frame, raw_rows, headers, settings=self.settings)
        result = dialog.result
        if not result:
            return

        try:
            backup_path = backup_database(get_db_path())
        except Exception:
            logging.exception("Не вдалося створити резервну копію БД перед імпортом товарів")
            show_error("Імпорт товарів", "Не вдалося створити резервну копію БД.")
            return

        try:
            summary = self._process_product_import(result["rows"], result["options"])
            summary = f"Резервну копію створено за шляхом:\n{backup_path}\n\n" + summary
        except Exception:
            logging.exception("Помилка під час імпорту товарів")
            show_error("Імпорт товарів", "Імпорт перервано помилкою. Деталі у логах.")
            return

        messagebox.showinfo("Імпорт товарів", summary)
        self.refresh_products()

    def open_product_stock_moves(self) -> None:
        product_id = self.product_table.selected_id()
        if not product_id:
            show_error("Рух товару", "Оберіть товар.")
            return
        open_stock_moves_dialog(self.frame.winfo_toplevel(), self.settings, int(product_id), None)

    def open_product_assemblies(self) -> None:
        product_id = self.product_table.selected_id()
        if not product_id:
            show_error("Комплектація", "Оберіть товар.")
            return
        product = db.get_product(int(product_id))
        if not product:
            show_error("Комплектація", "Товар не знайдено.")
            return
        ProductAssemblyConfigDialog(self.frame, product)

    def open_product_channel_codes(self) -> None:
        product_id = self.product_table.selected_id()
        if not product_id:
            show_error("Коди каналів", "Оберіть товар.")
            return
        product = db.get_product(int(product_id))
        if not product:
            show_error("Коди каналів", "Товар не знайдено.")
            return
        ProductChannelCodesDialog(self.frame, product)


class ProductChannelCodesDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, product: dict) -> None:
        super().__init__(parent)
        self.title(f"Коди каналів: {product.get('sku')} — {product.get('name')}")
        self.resizable(True, True)
        self.transient(parent.winfo_toplevel())
        self.grab_set()
        self.product_id = int(product["id"])
        self.channels = db.list_channels(active_only=False)
        self.codes: list[dict] = []

        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        columns = [
            ("channel", "Канал", 160),
            ("external_sku", "Зовнішній SKU", 150),
            ("active", "Активний", 80),
            ("primary", "Основний", 90),
            ("last_seen", "Останній імпорт", 140),
            ("note", "Нотатка", 200),
        ]
        self.table = TableFrame(main, columns, height=10)
        self.table.pack(fill=tk.BOTH, expand=True)
        self.table.on_double_click(self.edit_code)
        self.table.register_context_menu(self.edit_code, self.delete_code)

        btns = ttk.Frame(main)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="Додати", command=self.add_code).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Змінити", command=self.edit_code).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Видалити", command=self.delete_code).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Оновити", command=self.refresh_codes).pack(side=tk.RIGHT, padx=4)

        self.refresh_codes()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    def refresh_codes(self) -> None:
        try:
            self.codes = list(db.list_channel_codes_for_product(self.product_id))
        except Exception:
            logging.exception("Failed to load channel codes")
            show_error("Коди каналів", "Не вдалося завантажити коди каналів.")
            return
        self.table.set_rows(
            [
                {
                    "id": row["id"],
                    "channel": row["channel_name"],
                    "external_sku": row["external_sku"],
                    "active": "Так" if row["is_active"] else "Ні",
                    "primary": "Так" if row["is_primary"] else "Ні",
                    "last_seen": row["last_seen_at"] or "",
                    "note": row["note"] or "",
                }
                for row in self.codes
            ]
        )

    def _open_editor(self, existing: Optional[dict]) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Код каналу")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="Канал:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ch_names = [c["name"] for c in self.channels]
        ch_ids = [int(c["id"]) for c in self.channels]
        channel_var = tk.StringVar()
        channel_combo = ttk.Combobox(dlg, textvariable=channel_var, state="readonly", values=ch_names, width=40)
        channel_combo.grid(row=0, column=1, sticky="ew", padx=6, pady=4)

        ttk.Label(dlg, text="Зовнішній SKU:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        sku_var = tk.StringVar(value=existing.get("external_sku") if existing else "")
        ttk.Entry(dlg, textvariable=sku_var, width=40).grid(row=1, column=1, sticky="ew", padx=6, pady=4)

        ttk.Label(dlg, text="Назва у файлі (опц.):").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        name_var = tk.StringVar(value=existing.get("external_name") if existing else "")
        ttk.Entry(dlg, textvariable=name_var, width=40).grid(row=2, column=1, sticky="ew", padx=6, pady=4)

        active_var = tk.BooleanVar(value=bool(existing.get("is_active")) if existing else True)
        primary_var = tk.BooleanVar(value=bool(existing.get("is_primary")) if existing else True)
        ttk.Checkbutton(dlg, text="Активний", variable=active_var).grid(row=3, column=1, sticky="w", padx=6)
        ttk.Checkbutton(dlg, text="Основний", variable=primary_var).grid(row=4, column=1, sticky="w", padx=6)

        ttk.Label(dlg, text="Нотатка:").grid(row=5, column=0, sticky="nw", padx=6, pady=4)
        note_var = tk.StringVar(value=existing.get("note") if existing else "")
        ttk.Entry(dlg, textvariable=note_var, width=40).grid(row=5, column=1, sticky="ew", padx=6, pady=4)

        if existing:
            try:
                idx = ch_ids.index(int(existing["channel_id"]))
                channel_combo.current(idx)
            except ValueError:
                channel_combo.set("")
        elif ch_names:
            channel_combo.current(0)

        result: dict = {}

        def on_save() -> None:
            if not channel_var.get():
                show_error("Коди каналів", "Оберіть канал.")
                return
            try:
                ch_idx = ch_names.index(channel_var.get())
                channel_id = ch_ids[ch_idx]
            except ValueError:
                show_error("Коди каналів", "Канал не знайдено.")
                return
            external_sku = sku_var.get().strip()
            if not external_sku:
                show_error("Коди каналів", "Зовнішній SKU обов'язковий.")
                return
            try:
                db.upsert_channel_code(
                    channel_id,
                    self.product_id,
                    external_sku,
                    external_name=name_var.get().strip() or None,
                    is_primary=1 if primary_var.get() else 0,
                    is_active=1 if active_var.get() else 0,
                    note=note_var.get().strip() or None,
                )
            except ValueError as exc:
                show_error("Коди каналів", str(exc))
                return
            except Exception:
                logging.exception("Failed to save channel code")
                show_error("Коди каналів", "Не вдалося зберегти код.")
                return
            result["saved"] = True
            dlg.destroy()

        ttk.Button(dlg, text="Скасувати", command=dlg.destroy).grid(row=6, column=0, padx=6, pady=8, sticky="e")
        ttk.Button(dlg, text="Зберегти", command=on_save).grid(row=6, column=1, padx=6, pady=8, sticky="w")
        dlg.bind("<Return>", lambda _e: on_save())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.wait_window()
        if result.get("saved"):
            self.refresh_codes()

    def add_code(self) -> None:
        self._open_editor(None)

    def edit_code(self) -> None:
        selected = self.table.selected_id()
        if not selected:
            show_error("Коди каналів", "Оберіть рядок.")
            return
        existing = next((c for c in self.codes if str(c["id"]) == str(selected)), None)
        if not existing:
            show_error("Коди каналів", "Не знайдено запис.")
            return
        self._open_editor(existing)

    def delete_code(self) -> None:
        selected = self.table.selected_id()
        if not selected:
            show_error("Коди каналів", "Оберіть рядок.")
            return
        if not messagebox.askyesno("Коди каналів", "Видалити запис?"):
            return
        try:
            db.delete_channel_code(int(selected))
            self.refresh_codes()
        except Exception:
            logging.exception("Failed to delete channel code")
            show_error("Коди каналів", "Не вдалося видалити код.")

    def _process_product_import(self, rows: list[dict], options: dict) -> str:
        mode = options.get("mode", "create")
        create_missing = bool(options.get("create_missing", True))
        extra_mode = options.get("extra_categories_mode", "none")
        update_name = bool(options.get("update_name", True))

        default_brand = self.settings.get("defaults", "product", "brand") or "Імпорт"
        default_category = self.settings.get("defaults", "product", "category") or "Імпорт"
        default_unit = (self.settings.get("defaults", "product", "unit") or "pcs").strip() or "pcs"

        created = 0
        updated = 0
        skipped = 0
        errors: list[str] = []

        conn = db.get_connection()
        try:
            with db.safe_transaction(conn):
                product_rows = list(
                    conn.execute(
                        "SELECT id, sku, name, supplier_sku, brand_id, category_id, unit, is_active FROM Products"
                    )
                )
                products_by_sku = {r["sku"].lower(): dict(r) for r in product_rows if r["sku"]}
                products_by_name = {r["name"].lower(): dict(r) for r in product_rows if r["name"]}

                brands = list(conn.execute("SELECT id, name FROM Brands"))
                categories = list(conn.execute("SELECT id, name FROM Categories"))
                brands_by_id = {int(b["id"]): b for b in brands}
                categories_by_id = {int(c["id"]): c for c in categories}
                brands_by_name = {b["name"].lower(): b for b in brands}
                categories_by_name = {c["name"].lower(): c for c in categories}

                for idx, row in enumerate(rows, start=1):
                    sku = (row.get("sku") or "").strip()
                    name = (row.get("name") or "").strip()
                    supplier_sku = (row.get("supplier_sku") or "").strip()
                    unit = (row.get("unit") or "").strip()
                    is_active = row.get("is_active")
                    brand_val = (row.get("brand") or "").strip()
                    category_val = (row.get("category") or "").strip()
                    extra_categories = list(row.get("extra_categories") or [])

                    matched_by = None
                    product = None
                    if sku:
                        product = products_by_sku.get(sku.lower())
                        matched_by = "sku" if product else None
                    if not product and mode in {"update", "upsert"} and not sku and name:
                        product = products_by_name.get(name.lower())
                        matched_by = "name" if product else None

                    if mode == "create" and product:
                        errors.append(f"Рядок {idx}: SKU або назва вже існує")
                        skipped += 1
                        continue

                    if mode == "update" and not product:
                        skipped += 1
                        continue

                    brand_id = self.resolve_brand(
                        conn,
                        brand_val,
                        row.get("brand_id"),
                        idx,
                        create_missing,
                        brands_by_id,
                        brands_by_name,
                        errors,
                    )
                    category_id = self.resolve_category(
                        conn,
                        category_val,
                        row.get("category_id"),
                        idx,
                        create_missing,
                        categories_by_id,
                        categories_by_name,
                        errors,
                    )

                    if mode in {"create", "upsert"} and not product:
                        final_sku = sku.strip()
                        final_name = name.strip()
                        if not final_sku or not final_name:
                            errors.append(f"Рядок {idx}: Потрібні SKU і назва для створення")
                            skipped += 1
                            continue
                        if final_sku.lower() in products_by_sku:
                            errors.append(f"Рядок {idx}: SKU '{final_sku}' вже існує")
                            skipped += 1
                            continue
                        if final_name.lower() in products_by_name:
                            errors.append(f"Рядок {idx}: Назва '{final_name}' вже існує")
                            skipped += 1
                            continue

                        if not brand_id:
                            brand_id = self.resolve_brand(
                                conn,
                                default_brand,
                                None,
                                idx,
                                create_missing,
                                brands_by_id,
                                brands_by_name,
                                errors,
                            ) or None
                        if not category_id:
                            category_id = self.resolve_category(
                                conn,
                                default_category,
                                None,
                                idx,
                                create_missing,
                                categories_by_id,
                                categories_by_name,
                                errors,
                            )
                        if not category_id or not brand_id:
                            skipped += 1
                            continue

                        try:
                            cur = conn.execute(
                                "INSERT INTO Products (sku, supplier_sku, name, brand_id, category_id, unit, is_active) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    final_sku,
                                    supplier_sku or None,
                                    final_name,
                                    brand_id,
                                    category_id,
                                    unit or default_unit,
                                    1 if (is_active is None or is_active) else 0,
                                ),
                            )
                        except sqlite3.IntegrityError as exc:
                            errors.append(f"Рядок {idx}: Конфлікт унікальності ({exc})")
                            skipped += 1
                            continue

                        product_id = int(cur.lastrowid)
                        product_row = {
                            "id": product_id,
                            "sku": final_sku,
                            "name": final_name,
                            "supplier_sku": supplier_sku,
                            "brand_id": brand_id,
                            "category_id": category_id,
                            "unit": unit or default_unit,
                            "is_active": 1 if (is_active is None or is_active) else 0,
                        }
                        products_by_sku[final_sku.lower()] = product_row
                        products_by_name[final_name.lower()] = product_row
                        product = product_row
                        created += 1
                    elif product:
                        old_name = product.get("name")
                        updates: dict[str, object] = {}
                        if supplier_sku:
                            updates["supplier_sku"] = supplier_sku
                        if unit:
                            updates["unit"] = unit
                        if brand_id:
                            updates["brand_id"] = brand_id
                        if category_id:
                            updates["category_id"] = category_id
                        if is_active is not None:
                            updates["is_active"] = 1 if is_active else 0

                        can_rename = matched_by == "sku" and update_name and name
                        if can_rename:
                            name_exists = products_by_name.get(name.lower())
                            if name_exists and int(name_exists.get("id")) != int(product["id"]):
                                errors.append(f"Рядок {idx}: Назва '{name}' вже використовується")
                                skipped += 1
                                continue
                            updates["name"] = name

                        if updates:
                            set_clause = ", ".join(f"{k}=?" for k in updates.keys())
                            conn.execute(
                                f"UPDATE Products SET {set_clause} WHERE id=?",
                                (*updates.values(), product["id"]),
                            )
                            product.update(updates)
                            if updates.get("name"):
                                if old_name:
                                    products_by_name.pop(old_name.lower(), None)
                                products_by_name[name.lower()] = product
                            updated += 1

                    if not product:
                        continue

                    if extra_mode != "none":
                        if extra_mode == "replace":
                            conn.execute("DELETE FROM ProductCategoryLinks WHERE product_id=?", (product["id"],))
                        if extra_categories:
                            for cat_name in extra_categories:
                                cat_id = self.resolve_category(
                                    conn,
                                    cat_name,
                                    None,
                                    idx,
                                    create_missing,
                                    categories_by_id,
                                    categories_by_name,
                                    errors,
                                ) if cat_name else None
                                if not cat_id or cat_id == product.get("category_id"):
                                    continue
                                conn.execute(
                                    "INSERT OR IGNORE INTO ProductCategoryLinks (product_id, category_id) VALUES (?, ?)",
                                    (product["id"], cat_id),
                                )
                    if extra_mode != "none" and extra_mode == "replace" and not extra_categories:
                        pass
        finally:
            conn.close()

        summary = [
            f"Режим: {mode}",
            f"Створено: {created}",
            f"Оновлено: {updated}",
            f"Пропущено: {skipped}",
            f"Помилок: {len(errors)}",
        ]
        if errors:
            summary.append("Перші помилки:\n" + "\n".join(errors[:10]))
        return "\n".join(summary)

    def next_sort_order(self, conn, parent_id=None) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM Categories WHERE parent_id IS ?",
            (parent_id,),
        ).fetchone()
        return int(row[0]) + 1

    def resolve_brand(
        self,
        conn,
        name: str,
        brand_id: Optional[int],
        row_idx: int,
        create_missing: bool,
        brands_by_id: dict,
        brands_by_name: dict,
        errors: list[str],
    ) -> Optional[int]:
        if brand_id:
            if brand_id in brands_by_id:
                return brand_id
            errors.append(f"Рядок {row_idx}: ID бренду {brand_id} не знайдено")
            return None
        clean = (name or "").strip()
        if not clean:
            return None
        existing = brands_by_name.get(clean.lower())
        if existing:
            return int(existing["id"])
        if not create_missing:
            errors.append(f"Рядок {row_idx}: Бренд '{clean}' не знайдено")
            return None
        cur = conn.execute("INSERT INTO Brands (name) VALUES (?)", (clean,))
        brand_id = cur.lastrowid
        brand_row = {"id": brand_id, "name": clean}
        brands_by_id[int(brand_id)] = brand_row
        brands_by_name[clean.lower()] = brand_row
        return int(brand_id)

    def resolve_category(
        self,
        conn,
        name: str,
        category_id: Optional[int],
        row_idx: int,
        create_missing: bool,
        categories_by_id: dict,
        categories_by_name: dict,
        errors: list[str],
    ) -> Optional[int]:
        if category_id:
            if category_id in categories_by_id:
                return category_id
            errors.append(f"Рядок {row_idx}: Категорію з ID {category_id} не знайдено")
            return None
        clean = (name or "").strip()
        if not clean:
            return None
        existing = categories_by_name.get(clean.lower())
        if existing:
            return int(existing["id"])
        if not create_missing:
            errors.append(f"Рядок {row_idx}: Категорію '{clean}' не знайдено")
            return None
        sort_order = self.next_sort_order(conn, None)
        cur = conn.execute(
            "INSERT INTO Categories (name, parent_id, sort_order, is_service, is_hidden) VALUES (?, NULL, ?, 0, 0)",
            (clean, sort_order),
        )
        cat_id = cur.lastrowid
        cat_row = {"id": cat_id, "name": clean}
        categories_by_id[int(cat_id)] = cat_row
        categories_by_name[clean.lower()] = cat_row
        return int(cat_id)


class ProductAssemblyConfigDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, product: dict) -> None:
        super().__init__(parent)
        self.title(f"Комплектація товару: {product.get('sku')} — {product.get('name')}")
        self.resizable(True, True)
        self.transient(parent.winfo_toplevel())
        self.grab_set()
        self.product_id = int(product["id"])
        self.groups: list[dict] = []
        self.slots: list[dict] = []
        self.requirements: list[dict] = []
        self.all_slots: list[dict] = []
        self.all_groups: list[dict] = []

        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        info = ttk.Label(
            main,
            text=(
                "Членство в групі означає взаємозамінність. Покриття слотів означає, що товар може "
                "закривати один або кілька слотів (напр. набір 4в1 може покривати INSTALL_KIT та SCRAPER)."
            ),
            wraplength=640,
            justify="left",
        )
        info.pack(fill=tk.X, pady=(0, 8))

        notebook = ttk.Notebook(main)
        notebook.pack(fill=tk.BOTH, expand=True)

        group_frame = ttk.Frame(notebook)
        notebook.add(group_frame, text="Групи")
        group_columns = [
            ("code", "Код", 140),
            ("name", "Назва", 220),
            ("is_member", "У групі", 90),
            ("priority", "Пріоритет", 90),
        ]
        self.group_table = TableFrame(group_frame, group_columns, height=12)
        self.group_table.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.group_table.on_double_click(self._toggle_group_membership)

        slot_frame = ttk.Frame(notebook)
        notebook.add(slot_frame, text="Покриття")
        slot_columns = [
            ("code", "Код", 160),
            ("name", "Назва", 220),
            ("is_covered", "Покриває", 90),
        ]
        self.slot_table = TableFrame(slot_frame, slot_columns, height=12)
        self.slot_table.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.slot_table.on_double_click(self._toggle_slot_coverage)

        req_frame = ttk.Frame(notebook)
        notebook.add(req_frame, text="Вимоги (слоти)")
        req_columns = [
            ("slot_code", "Код слоту", 140),
            ("slot_name", "Назва слоту", 220),
            ("qty", "Кількість", 100),
            ("group_code", "Група", 140),
        ]
        self.requirement_table = TableFrame(req_frame, req_columns, height=12)
        self.requirement_table.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))
        self.requirement_table.on_double_click(self._edit_requirement)

        req_btns = ttk.Frame(req_frame)
        req_btns.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(req_btns, text="Додати", command=self._add_requirement).pack(side=tk.LEFT, padx=4)
        ttk.Button(req_btns, text="Змінити", command=self._edit_requirement).pack(side=tk.LEFT, padx=4)
        ttk.Button(req_btns, text="Видалити", command=self._delete_requirement).pack(side=tk.LEFT, padx=4)

        btns = ttk.Frame(main)
        btns.pack(fill=tk.X, pady=6)
        ttk.Button(btns, text="Підібрати компоненти…", command=self._preview_components).pack(
            side=tk.RIGHT, padx=4
        )
        ttk.Button(btns, text="Оновити", command=self.refresh_data).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Закрити", command=self.destroy).pack(side=tk.RIGHT, padx=4)

        self.refresh_data()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    def refresh_data(self) -> None:
        try:
            self.groups = db.list_all_groups_with_product_flag(self.product_id)
            self.slots = db.list_all_slots_with_product_flag(self.product_id)
            self.requirements = db.list_product_requirements(self.product_id)
            self.all_slots = db.list_assembly_slots()
            self.all_groups = db.list_component_groups()
        except Exception:
            logging.exception("Failed to load assemblies data for product")
            show_error("Комплектація", "Не вдалося завантажити дані комплектування.")
            return

        self.group_table.set_rows(
            [
                {
                    "id": g["id"],
                    "code": g.get("code"),
                    "name": g.get("name"),
                    "is_member": "Так" if g.get("is_member") else "Ні",
                    "priority": g.get("priority") if g.get("is_member") else "",
                }
                for g in self.groups
            ]
        )
        self.slot_table.set_rows(
            [
                {
                    "id": s["id"],
                    "code": s.get("code"),
                    "name": s.get("name"),
                    "is_covered": "Так" if s.get("is_covered") else "Ні",
                }
                for s in self.slots
            ]
        )
        self.requirement_table.set_rows(
            [
                {
                    "id": r["slot_id"],
                    "slot_code": r.get("slot_code"),
                    "slot_name": r.get("slot_name"),
                    "qty": r.get("qty"),
                    "group_code": r.get("group_code") or "",
                }
                for r in self.requirements
            ]
        )

    def _toggle_group_membership(self) -> None:
        selection = self.group_table.selected_id()
        if selection is None:
            show_error("Комплектація", "Оберіть групу для зміни.")
            return
        group = next((g for g in self.groups if int(g["id"]) == int(selection)), None)
        if not group:
            return
        try:
            if group.get("is_member"):
                db.set_product_in_component_group(self.product_id, int(group["id"]), False)
            else:
                default_priority = group.get("priority") or 100
                values = simple_prompt(
                    "Пріоритет (менше = краще)",
                    ["Пріоритет"],
                    [str(default_priority)],
                )
                if not values:
                    return
                try:
                    priority = int(values[0])
                except (TypeError, ValueError):
                    show_error("Комплектація", "Пріоритет має бути цілим числом.")
                    return
                db.set_product_in_component_group(self.product_id, int(group["id"]), True, priority)
        except sqlite3.IntegrityError:
            show_error("Комплектація", "Не вдалося оновити групу. Перевірте унікальність коду.")
        except Exception:
            logging.exception("Failed to toggle group membership")
            show_error("Комплектація", "Не вдалося змінити налаштування групи.")
        finally:
            self.refresh_data()

    def _toggle_slot_coverage(self) -> None:
        selection = self.slot_table.selected_id()
        if selection is None:
            show_error("Комплектація", "Оберіть слот для зміни.")
            return
        slot = next((s for s in self.slots if int(s["id"]) == int(selection)), None)
        if not slot:
            return
        try:
            db.set_product_slot_covered(self.product_id, int(slot["id"]), not bool(slot.get("is_covered")))
        except sqlite3.IntegrityError:
            show_error("Комплектація", "Не вдалося оновити покриття слоту через конфлікт унікальності.")
        except Exception:
            logging.exception("Failed to toggle slot coverage")
            show_error("Комплектація", "Не вдалося змінити покриття слоту.")
        finally:
            self.refresh_data()

    def _get_slot_by_id(self, slot_id: int) -> Optional[dict]:
        return next((s for s in self.all_slots if int(s["id"]) == int(slot_id)), None)

    def _add_requirement(self) -> None:
        self._open_requirement_editor(None)

    def _edit_requirement(self) -> None:
        selection = self.requirement_table.selected_id()
        if selection is None:
            show_error("Комплектація", "Оберіть вимогу для редагування.")
            return
        req = next((r for r in self.requirements if int(r["slot_id"]) == int(selection)), None)
        if req is None:
            show_error("Комплектація", "Не знайдено дані вимоги.")
            return
        self._open_requirement_editor(req)

    def _open_requirement_editor(self, existing: Optional[dict]) -> None:
        if not self.all_slots:
            show_error("Вимоги", "Немає доступних слотів.")
            return

        dlg = tk.Toplevel(self)
        dlg.title("Вимога по слоту")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="Слот:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        slot_options = [f"{s.get('code')} — {s.get('name')}" for s in self.all_slots]
        slot_ids = [int(s["id"]) for s in self.all_slots]
        slot_var = tk.StringVar()
        slot_combo = ttk.Combobox(dlg, textvariable=slot_var, values=slot_options, state="readonly", width=40)
        slot_combo.grid(row=0, column=1, sticky="w", padx=8, pady=6)

        ttk.Label(dlg, text="Кількість на 1 шт.:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        qty_var = tk.StringVar(value=str(existing.get("qty") if existing else "1"))
        qty_entry = ttk.Entry(dlg, textvariable=qty_var, width=12)
        qty_entry.grid(row=1, column=1, sticky="w", padx=8, pady=6)

        ttk.Label(dlg, text="Бажана група:").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        group_options = ["(будь-яка) / None"] + [f"{g.get('code')} — {g.get('name')}" for g in self.all_groups]
        group_ids = [None] + [int(g["id"]) for g in self.all_groups]
        group_var = tk.StringVar()
        group_combo = ttk.Combobox(dlg, textvariable=group_var, values=group_options, state="readonly", width=40)
        group_combo.grid(row=2, column=1, sticky="w", padx=8, pady=6)

        if existing:
            try:
                idx = slot_ids.index(int(existing["slot_id"]))
                slot_combo.current(idx)
            except ValueError:
                slot_combo.set("")
            if existing.get("group_id") is not None:
                try:
                    gidx = group_ids.index(int(existing["group_id"]))
                    group_combo.current(gidx)
                except ValueError:
                    group_combo.set("")
            else:
                group_combo.current(0)
            slot_combo.state(["disabled"])
        else:
            if slot_options:
                slot_combo.current(0)
            group_combo.current(0)

        result: dict = {}

        def on_ok() -> None:
            try:
                qty_value = float(qty_var.get())
            except ValueError:
                show_error("Вимоги", "Кількість має бути числом.")
                return
            if qty_value <= 0:
                show_error("Вимоги", "Кількість має бути більшою за 0.")
                return

            try:
                slot_idx = slot_options.index(slot_var.get())
                slot_id = slot_ids[slot_idx]
            except ValueError:
                show_error("Вимоги", "Оберіть слот.")
                return

            group_id: Optional[int] = None
            if group_var.get() in group_options and group_options.index(group_var.get()) > 0:
                group_id = group_ids[group_options.index(group_var.get())]

            try:
                db.upsert_product_requirement(self.product_id, int(slot_id), qty_value, group_id)
            except sqlite3.IntegrityError:
                show_error("Вимоги", "Не вдалося зберегти вимогу. Перевірте унікальність.")
                return
            except Exception:
                logging.exception("Failed to save product requirement")
                show_error("Вимоги", "Не вдалося зберегти вимогу.")
                return
            result["saved"] = True
            dlg.destroy()

        ttk.Button(dlg, text="Скасувати", command=dlg.destroy).grid(row=3, column=0, padx=8, pady=8, sticky="e")
        ttk.Button(dlg, text="Зберегти", command=on_ok).grid(row=3, column=1, padx=8, pady=8, sticky="w")
        dlg.bind("<Return>", lambda _e: on_ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        qty_entry.focus_set()
        dlg.wait_window()
        if result.get("saved"):
            self.refresh_data()

    def _delete_requirement(self) -> None:
        selection = self.requirement_table.selected_id()
        if selection is None:
            show_error("Комплектація", "Оберіть вимогу для видалення.")
            return
        slot = self._get_slot_by_id(int(selection))
        confirm_label = slot.get("code") if slot else str(selection)
        if not messagebox.askyesno("Вимоги", f"Видалити вимогу для слоту {confirm_label}?"):
            return
        try:
            db.delete_product_requirement(self.product_id, int(selection))
        except Exception:
            logging.exception("Failed to delete requirement")
            show_error("Вимоги", "Не вдалося видалити вимогу.")
        finally:
            self.refresh_data()

    def _preview_components(self) -> None:
        params = self._ask_preview_params()
        if not params:
            return
        qty, warehouse_id = params
        try:
            result = assembly_resolver.resolve_components_for_product(self.product_id, warehouse_id, qty)
        except Exception:
            logging.exception("Failed to resolve components")
            show_error("Підбір компонентів", "Не вдалося підібрати компоненти.")
            return

        lines: list[str] = []
        components = result.get("components") or []
        if components:
            lines.append("Компоненти:")
            for comp in components:
                lines.append(f"- {comp.get('sku')}: {comp.get('name')} — {comp.get('qty')}")
        else:
            lines.append("Компоненти не знайдені.")

        missing_slots = result.get("missing_slots") or []
        if missing_slots:
            lines.append("")
            lines.append("НЕ ВИСТАЧАЄ:")
            for miss in missing_slots:
                label = miss.get("slot_code") or miss.get("slot_name") or "slot"
                lines.append(f"- {label}: {miss.get('missing_qty')}")

        messagebox.showinfo("Підібрати компоненти", "\n".join(lines), parent=self)

    def _ask_preview_params(self) -> Optional[tuple[float, int]]:
        warehouses = db.list_warehouses(active_only=True)
        if not warehouses:
            show_error("Підбір компонентів", "Немає доступних складів.")
            return None
        if len(warehouses) == 1:
            values = simple_prompt(
                "Підібрати компоненти",
                ["Кількість готових одиниць"],
                ["1"],
            )
            if not values:
                return None
            try:
                qty = float(values[0])
            except ValueError:
                show_error("Підбір компонентів", "Кількість має бути числом.")
                return None
            if qty <= 0:
                show_error("Підбір компонентів", "Кількість має бути більшою за 0.")
                return None
            return qty, int(warehouses[0]["id"])

        dlg = tk.Toplevel(self)
        dlg.title("Параметри підбору")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="Кількість готових одиниць:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        qty_var = tk.StringVar(value="1")
        qty_entry = ttk.Entry(dlg, textvariable=qty_var, width=12)
        qty_entry.grid(row=0, column=1, sticky="w", padx=8, pady=6)

        ttk.Label(dlg, text="Склад:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        wh_options = [f"{w['name']} (#{w['id']})" for w in warehouses]
        wh_ids = [int(w["id"]) for w in warehouses]
        wh_var = tk.StringVar(value=wh_options[0])
        wh_combo = ttk.Combobox(dlg, textvariable=wh_var, values=wh_options, state="readonly", width=30)
        wh_combo.grid(row=1, column=1, sticky="w", padx=8, pady=6)

        result: dict = {}

        def on_ok() -> None:
            try:
                qty_val = float(qty_var.get())
            except ValueError:
                show_error("Підбір компонентів", "Кількість має бути числом.")
                return
            if qty_val <= 0:
                show_error("Підбір компонентів", "Кількість має бути більшою за 0.")
                return
            try:
                wh_idx = wh_options.index(wh_var.get())
                wh_id = wh_ids[wh_idx]
            except ValueError:
                show_error("Підбір компонентів", "Оберіть склад.")
                return
            result["qty"] = qty_val
            result["warehouse_id"] = wh_id
            dlg.destroy()

        ttk.Button(dlg, text="Скасувати", command=dlg.destroy).grid(row=2, column=0, padx=8, pady=10, sticky="e")
        ttk.Button(dlg, text="OK", command=on_ok).grid(row=2, column=1, padx=8, pady=10, sticky="w")
        dlg.bind("<Return>", lambda _e: on_ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        qty_entry.focus_set()
        dlg.wait_window()

        if "qty" in result and "warehouse_id" in result:
            return float(result["qty"]), int(result["warehouse_id"])
        return None


class ProductsImportDialog(tk.Toplevel):
    def __init__(self, app: tk.Misc, raw_rows: list[dict[str, object]], headers: list[str], settings: Settings) -> None:
        super().__init__(app)
        self.title("Імпорт товарів")
        self.resizable(True, True)
        self.grab_set()
        self.result: Optional[dict] = None
        self.raw_rows = raw_rows
        self.headers = headers
        self.settings = settings
        self.templates: dict[str, dict[str, str]] = settings.get("product_import", "templates") or {}
        self.current_mapping = _suggest_product_mapping(headers)

        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        info = ttk.Label(main, text=f"Рядків у файлі: {len(raw_rows)}")
        info.grid(row=0, column=0, columnspan=3, sticky="w")

        self._build_template_controls(main)
        self._build_mapping_controls(main)
        self._build_options(main)
        self.preview = self._build_preview(main)
        self._refresh_preview()

        btns = ttk.Frame(main)
        btns.grid(row=11, column=0, columnspan=3, pady=8, sticky="e")
        ttk.Button(btns, text="Скасувати", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Імпортувати", command=self._on_ok).pack(side=tk.RIGHT, padx=4)

        self.bind("<Return>", lambda _e: self._on_ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.wait_window(self)

    def _on_ok(self) -> None:
        normalized_rows = _normalize_product_records(self.raw_rows, self.current_mapping)
        self.result = {
            "mapping": dict(self.current_mapping),
            "options": {
                "mode": self.mode_var.get(),
                "create_missing": bool(self.create_missing_var.get()),
                "extra_categories_mode": self.extra_categories_mode.get(),
                "update_name": bool(self.update_name_var.get()),
            },
            "rows": normalized_rows,
        }
        self.settings.set(self.current_template_name.get(), "product_import", "last_template")
        self.settings.save()
        self.destroy()

    def _build_template_controls(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Шаблон співставлення:").grid(row=1, column=0, sticky="w", pady=4)
        self.current_template_name = tk.StringVar(value=self.settings.get("product_import", "last_template") or "")
        self.template_combo = ttk.Combobox(
            parent, textvariable=self.current_template_name, values=list(self.templates.keys()), state="readonly"
        )
        self.template_combo.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="Застосувати", command=self._apply_template).grid(row=1, column=2, padx=4, sticky="w")
        ttk.Button(parent, text="Зберегти", command=self._save_template).grid(row=1, column=3, padx=4, sticky="w")
        ttk.Button(parent, text="Видалити", command=self._delete_template).grid(row=1, column=4, padx=4, sticky="w")

    def _build_mapping_controls(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Співставлення колонок:").grid(row=2, column=0, sticky="nw", pady=4)
        mapping_frame = ttk.Frame(parent)
        mapping_frame.grid(row=2, column=1, columnspan=4, sticky="ew", pady=4)
        mapping_frame.columnconfigure(1, weight=1)

        options = ["(не використовувати)"] + self.headers
        self.mapping_vars: dict[str, tk.StringVar] = {}
        for idx, (field_key, field_label, _aliases) in enumerate(PRODUCT_FIELDS):
            ttk.Label(mapping_frame, text=field_label).grid(row=idx, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=self.current_mapping.get(field_key, ""))
            combo = ttk.Combobox(mapping_frame, textvariable=var, values=options, state="readonly")
            combo.grid(row=idx, column=1, sticky="ew", pady=2)
            combo.bind("<<ComboboxSelected>>", lambda _e, key=field_key, v=var: self._update_mapping(key, v.get()))
            self.mapping_vars[field_key] = var

    def _build_options(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Режим імпорту:").grid(row=3, column=0, sticky="nw", pady=4)
        mode_frame = ttk.Frame(parent)
        mode_frame.grid(row=3, column=1, sticky="w", pady=4)
        self.mode_var = tk.StringVar(value="create")
        ttk.Radiobutton(mode_frame, text="Створювати", variable=self.mode_var, value="create").pack(anchor="w")
        ttk.Radiobutton(mode_frame, text="Оновлювати", variable=self.mode_var, value="update").pack(anchor="w")
        ttk.Radiobutton(mode_frame, text="Додавати/оновлювати", variable=self.mode_var, value="upsert").pack(anchor="w")

        self.create_missing_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            parent,
            text="Створювати відсутні бренди/категорії",
            variable=self.create_missing_var,
        ).grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(parent, text="Додаткові категорії:").grid(row=5, column=0, sticky="nw", pady=4)
        extra_frame = ttk.Frame(parent)
        extra_frame.grid(row=5, column=1, sticky="w")
        self.extra_categories_mode = tk.StringVar(value="none")
        ttk.Radiobutton(extra_frame, text="Не чіпати", variable=self.extra_categories_mode, value="none").pack(anchor="w")
        ttk.Radiobutton(extra_frame, text="Додати", variable=self.extra_categories_mode, value="add").pack(anchor="w")
        ttk.Radiobutton(extra_frame, text="Замінити", variable=self.extra_categories_mode, value="replace").pack(anchor="w")

        self.update_name_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(parent, text="Оновлювати назву товару", variable=self.update_name_var).grid(
            row=6, column=1, sticky="w", pady=4
        )

    def _build_preview(self, parent: ttk.Frame) -> ttk.Treeview:
        ttk.Label(parent, text="Попередній перегляд (перші 50 рядків):").grid(row=7, column=0, columnspan=4, sticky="w", pady=6)
        preview = ttk.Treeview(
            parent,
            columns=("sku", "name", "brand", "category", "unit", "active", "extras"),
            show="headings",
            height=12,
        )
        headings = {
            "sku": ("SKU", 120),
            "name": ("Назва", 180),
            "brand": ("Бренд", 140),
            "category": ("Категорія", 140),
            "unit": ("Одиниця", 90),
            "active": ("Активний", 90),
            "extras": ("Додаткові категорії", 220),
        }
        for col, (title, width) in headings.items():
            preview.heading(col, text=title)
            preview.column(col, width=width, anchor="w")
        preview.grid(row=8, column=0, columnspan=4, sticky="nsew")
        parent.grid_rowconfigure(8, weight=1)
        parent.grid_columnconfigure(1, weight=1)
        scroll = ttk.Scrollbar(parent, orient="vertical", command=preview.yview)
        preview.configure(yscrollcommand=scroll.set)
        scroll.grid(row=8, column=4, sticky="ns")
        return preview

    def _refresh_preview(self) -> None:
        self.preview.delete(*self.preview.get_children())
        normalized = _normalize_product_records(self.raw_rows, self.current_mapping)
        for row in normalized[:50]:
            self.preview.insert(
                "",
                "end",
                values=(
                    row.get("sku"),
                    row.get("name"),
                    row.get("brand"),
                    row.get("category"),
                    row.get("unit"),
                    row.get("is_active"),
                    "; ".join(row.get("extra_categories") or []),
                ),
            )

    def _update_mapping(self, key: str, value: str) -> None:
        clean_value = "" if value == "(не використовувати)" else value
        self.current_mapping[key] = clean_value
        self._refresh_preview()

    def _apply_template(self) -> None:
        name = self.current_template_name.get().strip()
        if not name or name not in self.templates:
            return
        template = self.templates[name]
        for key, var in self.mapping_vars.items():
            var.set(template.get(key, ""))
            self.current_mapping[key] = template.get(key, "")
        self._refresh_preview()

    def _save_template(self) -> None:
        values = simple_prompt(
            "Назва шаблону",
            ["Вкажіть назву шаблону"],
            [self.current_template_name.get().strip()],
        )
        if not values:
            return

        name = values[0].strip()
        if not name:
            return

        self.templates[name] = dict(self.current_mapping)
        self.settings.set(self.templates, "product_import", "templates")
        self.settings.set(name, "product_import", "last_template")
        self.settings.save()
        self.current_template_name.set(name)
        self.template_combo.configure(values=list(self.templates.keys()))

    def _delete_template(self) -> None:
        name = self.current_template_name.get().strip()
        if not name or name not in self.templates:
            return
        if not messagebox.askyesno("Шаблони", f"Видалити шаблон '{name}'?"):
            return
        self.templates.pop(name, None)
        self.settings.set(self.templates, "product_import", "templates")
        if self.settings.get("product_import", "last_template") == name:
            self.settings.set("", "product_import", "last_template")
        self.settings.save()
        self.template_combo.configure(values=list(self.templates.keys()))
        if self.templates:
            self.current_template_name.set(list(self.templates.keys())[0])
        else:
            self.current_template_name.set("")


def open_products_bulk_actions_dialog(parent, db_conn, table_frame=None, product_ids=None) -> None:
    if product_ids is None:
        if table_frame is None:
            messagebox.showwarning("Масові дії", "Оберіть хоча б один товар.")
            try:
                db_conn.close()
            except Exception:
                pass
            return
        product_ids = table_frame.get_selected_row_ids()
    if not product_ids:
        messagebox.showwarning("Масові дії", "Оберіть хоча б один товар.")
        try:
            db_conn.close()
        except Exception:
            pass
        return

    dlg = tk.Toplevel(getattr(parent, "frame", parent))
    dlg.title("Масові дії з товарами")
    dlg.grab_set()
    dlg.resizable(False, False)

    ttk.Label(dlg, text=f"Обрано товарів: {len(product_ids)}").grid(
        row=0, column=0, columnspan=3, padx=8, pady=(8, 4), sticky="w"
    )

    brands = db.list_brands()
    brand_names = [b["name"] for b in brands]
    categories = parent.flatten_categories()
    category_names = [c["label"] for c in categories]

    default_unit = "pcs"
    try:
        default_unit = (parent.settings.get("defaults", "product", "unit") or default_unit).strip() or "pcs"
    except Exception:
        default_unit = "pcs"

    def close_dialog() -> None:
        try:
            db_conn.close()
        except Exception:
            pass
        dlg.destroy()

    def confirm_and_apply(action_label: str, func) -> None:
        if not messagebox.askyesno("Підтвердження", f"Застосувати до {len(product_ids)} товарів?"):
            return
        try:
            affected = func()
        except Exception:
            logging.exception("Bulk products action error")
            show_error("Масові дії", "Не вдалося виконати дію.")
            return
        messagebox.showinfo("Масові дії", f"{action_label}: {affected}")
        parent.refresh_products()

    # Activation
    act_frame = ttk.LabelFrame(dlg, text="Активація")
    act_frame.grid(row=1, column=0, columnspan=3, padx=8, pady=4, sticky="ew")
    ttk.Button(act_frame, text="Активувати", command=lambda: confirm_and_apply(
        "Оновлено товарів",
        lambda: db.bulk_update_products_is_active(db_conn, product_ids, 1),
    )).pack(side=tk.LEFT, padx=4, pady=4)
    ttk.Button(act_frame, text="Деактивувати", command=lambda: confirm_and_apply(
        "Оновлено товарів",
        lambda: db.bulk_update_products_is_active(db_conn, product_ids, 0),
    )).pack(side=tk.LEFT, padx=4, pady=4)

    # Brand
    brand_frame = ttk.LabelFrame(dlg, text="Встановити бренд")
    brand_frame.grid(row=2, column=0, columnspan=3, padx=8, pady=4, sticky="ew")
    ttk.Label(brand_frame, text="Бренд:").pack(side=tk.LEFT, padx=4, pady=4)
    brand_var = tk.StringVar()
    brand_combo = ttk.Combobox(brand_frame, textvariable=brand_var, state="readonly", values=brand_names, width=30)
    brand_combo.pack(side=tk.LEFT, padx=4, pady=4)
    if brand_names:
        brand_combo.current(0)

    def apply_brand() -> None:
        if not brands:
            messagebox.showwarning("Бренди", "Створіть принаймні один бренд.")
            return
        confirm_and_apply(
            "Оновлено товарів",
            lambda: db.bulk_update_products_brand(db_conn, product_ids, brands[brand_combo.current()]["id"]),
        )

    ttk.Button(brand_frame, text="Застосувати бренд", command=apply_brand).pack(side=tk.LEFT, padx=4, pady=4)

    # Category
    category_frame = ttk.LabelFrame(dlg, text="Встановити категорію")
    category_frame.grid(row=3, column=0, columnspan=3, padx=8, pady=4, sticky="ew")
    ttk.Label(category_frame, text="Категорія:").pack(side=tk.LEFT, padx=4, pady=4)
    category_var = tk.StringVar()
    category_combo = ttk.Combobox(category_frame, textvariable=category_var, values=category_names, width=40)
    category_combo.pack(side=tk.LEFT, padx=4, pady=4)
    if category_names:
        category_combo.current(0)

    def apply_category() -> None:
        selected_label = category_var.get().strip()
        matched = next((c for c in categories if c["label"] == selected_label), None)
        if not matched:
            matched = next((c for c in categories if selected_label.lower() in c["label"].lower()), None)
        if not matched:
            messagebox.showwarning("Категорії", "Оберіть категорію.")
            return
        confirm_and_apply(
            "Оновлено товарів",
            lambda: db.bulk_update_products_category(db_conn, product_ids, matched["id"]),
        )

    ttk.Button(category_frame, text="Застосувати категорію", command=apply_category).pack(
        side=tk.LEFT, padx=4, pady=4
    )

    # Unit
    unit_frame = ttk.LabelFrame(dlg, text="Одиниця")
    unit_frame.grid(row=4, column=0, columnspan=3, padx=8, pady=4, sticky="ew")
    ttk.Label(unit_frame, text="Одиниця:").pack(side=tk.LEFT, padx=4, pady=4)
    unit_var = tk.StringVar(value=default_unit)
    ttk.Entry(unit_frame, textvariable=unit_var, width=10).pack(side=tk.LEFT, padx=4, pady=4)
    ttk.Button(
        unit_frame,
        text="Застосувати одиницю",
        command=lambda: confirm_and_apply(
            "Оновлено товарів",
            lambda: db.bulk_update_products_unit(db_conn, product_ids, unit_var.get()),
        ),
    ).pack(side=tk.LEFT, padx=4, pady=4)

    # Extra categories
    extras_frame = ttk.LabelFrame(dlg, text="Додаткові категорії")
    extras_frame.grid(row=5, column=0, columnspan=3, padx=8, pady=4, sticky="ew")
    extras_frame.columnconfigure(0, weight=1)
    extras_box = tk.Listbox(extras_frame, selectmode=tk.MULTIPLE, height=min(10, max(6, len(categories))), exportselection=False)
    for cat in categories:
        extras_box.insert(tk.END, cat["label"])
    extras_box.grid(row=0, column=0, rowspan=2, padx=4, pady=4, sticky="nsew")
    scroll = ttk.Scrollbar(extras_frame, orient="vertical", command=extras_box.yview)
    extras_box.configure(yscrollcommand=scroll.set)
    scroll.grid(row=0, column=1, rowspan=2, sticky="ns", pady=4)

    def selected_extra_ids() -> list[int]:
        return [categories[i]["id"] for i in extras_box.curselection() if i < len(categories)]

    def apply_extra_add() -> None:
        ids = selected_extra_ids()
        if not ids:
            messagebox.showwarning("Категорії", "Оберіть додаткові категорії.")
            return
        confirm_and_apply(
            "Додано зв'язків",
            lambda: db.bulk_add_product_category_links(db_conn, product_ids, ids),
        )

    def apply_extra_remove() -> None:
        ids = selected_extra_ids()
        if not ids:
            messagebox.showwarning("Категорії", "Оберіть додаткові категорії.")
            return
        confirm_and_apply(
            "Видалено зв'язків",
            lambda: db.bulk_remove_product_category_links(db_conn, product_ids, ids),
        )

    ttk.Button(extras_frame, text="Додати категорії", command=apply_extra_add).grid(
        row=0, column=2, padx=6, pady=4, sticky="n"
    )

    ttk.Button(extras_frame, text="Прибрати категорії", command=apply_extra_remove).grid(
        row=1, column=2, padx=6, pady=4, sticky="n"
    )

    labels_frame = ttk.LabelFrame(dlg, text="Етикетки (Code128)")
    labels_frame.grid(row=6, column=0, columnspan=3, padx=8, pady=4, sticky="ew")
    ttk.Label(labels_frame, text="Шаблон:").pack(side=tk.LEFT, padx=4, pady=4)
    template_display_var = tk.StringVar()
    template_combo = ttk.Combobox(labels_frame, textvariable=template_display_var, state="readonly", width=26)
    template_combo.pack(side=tk.LEFT, padx=4, pady=4)
    template_map: dict[str, int] = {}
    templates_cache: dict[int, dict] = {}

    start_row_var = tk.StringVar(value="1")
    start_col_var = tk.StringVar(value="1")
    start_frame = ttk.Frame(labels_frame)
    start_row_label = ttk.Label(start_frame, text="Ряд:")
    start_row_spin = ttk.Spinbox(start_frame, from_=1, to=1, textvariable=start_row_var, width=4)
    start_col_label = ttk.Label(start_frame, text="Кол:")
    start_col_spin = ttk.Spinbox(start_frame, from_=1, to=1, textvariable=start_col_var, width=4)

    def refresh_template_choices(selected_id: int | None = None) -> None:
        nonlocal templates_cache
        template_map.clear()
        templates_cache = {}
        try:
            templates = db.list_label_templates(active_only=True)
        except Exception:
            logging.exception("Failed to load label templates")
            show_error("Етикетки", "Не вдалося завантажити шаблони")
            return
        display_values: list[str] = []
        default_display: str | None = None
        last_selected_id = None
        try:
            last_selected_id = int(parent.settings.get("print", "last_template_id") or 0)
        except Exception:
            last_selected_id = None
        for row in templates:
            tpl = dict(row)
            display = f"{tpl['title']} [{tpl['code']}]"
            display_values.append(display)
            tid = int(tpl["id"])
            template_map[display] = tid
            templates_cache[tid] = tpl
            if int(tpl.get("is_default") or 0) == 1:
                default_display = display
        template_combo.configure(values=display_values)
        target_id = selected_id or last_selected_id
        if target_id and target_id in template_map.values():
            for disp, tid in template_map.items():
                if tid == target_id:
                    template_display_var.set(disp)
                    break
        elif default_display:
            template_display_var.set(default_display)
        elif display_values:
            template_display_var.set(display_values[0])
        update_start_controls()

    def update_start_controls(*_args) -> None:
        display = template_display_var.get()
        tpl_id = template_map.get(display)
        tpl_row = templates_cache.get(tpl_id or -1)
        is_sheet = tpl_row and (tpl_row.get("kind") == "sheet")
        for widget in [start_row_label, start_row_spin, start_col_label, start_col_spin, start_frame]:
            widget.pack_forget()
        if is_sheet:
            rows = max(int(tpl_row.get("rows", 1)), 1)
            cols = max(int(tpl_row.get("cols", 1)), 1)
            start_row_spin.configure(to=rows)
            start_col_spin.configure(to=cols)
            start_row_label.pack(side=tk.LEFT, padx=2)
            start_row_spin.pack(side=tk.LEFT, padx=2)
            start_col_label.pack(side=tk.LEFT, padx=2)
            start_col_spin.pack(side=tk.LEFT, padx=2)
            start_frame.pack(side=tk.LEFT, padx=4, pady=4)
        else:
            start_row_var.set("1")
            start_col_var.set("1")

    template_combo.bind("<<ComboboxSelected>>", update_start_controls)

    def open_template_manager() -> None:
        dlg_manager = TemplateManagerDialog(parent, settings=getattr(parent, "settings", None))
        dlg.wait_window(dlg_manager.root)
        refresh_template_choices()

    ttk.Button(labels_frame, text="Шаблони…", command=open_template_manager).pack(side=tk.LEFT, padx=4, pady=4)

    ttk.Label(labels_frame, text="К-сть етикеток на товар:").pack(side=tk.LEFT, padx=4, pady=4)
    qty_var = tk.StringVar(value="1")
    ttk.Spinbox(labels_frame, from_=1, to=999, textvariable=qty_var, width=5).pack(side=tk.LEFT, padx=4, pady=4)

    include_aliases_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(labels_frame, text="Друкувати також додаткові штрихкоди (аліаси)", variable=include_aliases_var).pack(
        side=tk.LEFT, padx=4, pady=4
    )

    refresh_template_choices()

    def generate_labels() -> None:
        try:
            qty_each = int(qty_var.get())
        except ValueError:
            show_error("Етикетки", "Вкажіть кількість етикеток числом")
            return
        if qty_each <= 0:
            show_error("Етикетки", "Кількість має бути більшою за 0")
            return

        file_path = filedialog.asksaveasfilename(
            title="Файл PDF з етикетками",
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf"), ("Усі файли", "*.*")],
            initialdir=str(parent.default_workdir()),
        )
        if not file_path:
            return

        try:
            placeholders = ",".join("?" * len(product_ids))
            rows = db_conn.execute(
                f"SELECT id, sku, name FROM Products WHERE id IN ({placeholders})",
                tuple(product_ids),
            ).fetchall()
            if not rows:
                show_error("Етикетки", "Не знайдено жодного товару")
                return
            alias_map: dict[int, list[str]] = defaultdict(list)
            if include_aliases_var.get():
                for alias_row in db_conn.execute(
                    f"SELECT product_id, code FROM ProductBarcodes WHERE product_id IN ({placeholders})",
                    tuple(product_ids),
                ).fetchall():
                    alias_map[int(alias_row["product_id"])].append(alias_row["code"])

            prefix = _sanitize_barcode_prefix(parent.settings.get("defaults", "product", "barcode_prefix") or "")
            items = [
                {
                    "product_id": row["id"],
                    "sku": row["sku"],
                    "name": row["name"],
                    "aliases": alias_map.get(int(row["id"]), []),
                }
                for row in rows
            ]
            tpl_display = template_display_var.get()
            tpl_id = template_map.get(tpl_display)
            if not tpl_id:
                show_error("Етикетки", "Оберіть шаблон")
                return
            template_full = db.get_label_template_full(tpl_id)
            if not template_full:
                show_error("Етикетки", "Шаблон не знайдено")
                return
            try:
                start_row = max(1, int(start_row_var.get() or 1))
                start_col = max(1, int(start_col_var.get() or 1))
            except ValueError:
                start_row = start_col = 1
            labels.generate_product_labels_pdf_v2(
                Path(file_path),
                items,
                barcode_prefix=prefix,
                qty_each=qty_each,
                template_full=template_full,
                include_aliases=bool(include_aliases_var.get()),
                start_row=start_row,
                start_col=start_col,
            )
            try:
                parent.settings.set(tpl_id, "print", "last_template_id")
                parent.settings.save()
            except Exception:
                logging.exception("Failed to save template selection")
            open_file(Path(file_path))
        except Exception:
            logging.exception("Labels generation error")
            show_error("Етикетки", "Не вдалося згенерувати етикетки")

    ttk.Button(labels_frame, text="Згенерувати PDF...", command=generate_labels).pack(side=tk.LEFT, padx=6, pady=4)

    ttk.Button(dlg, text="Закрити", command=close_dialog).grid(row=7, column=0, columnspan=3, pady=8)
    dlg.protocol("WM_DELETE_WINDOW", close_dialog)
