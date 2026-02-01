"""SQLite database helpers for InventoryLite with cash-basis accounting.

This module stores reference data (brands, categories, products, warehouses,
channels, counterparties) and operational documents (purchases, sales,
cash transactions). Inventory is valued using moving-average cost per
product and warehouse. Income and expenses are recognized only when cash
actually moves (cash basis). Direct-costing is applied: COGS includes only
variable costs from purchase price; fixed costs are captured via cash
transactions but are not added into inventory cost.
"""
from __future__ import annotations

import json
import logging
import math
import os
import platform
import re
import sqlite3
from contextlib import contextmanager, nullcontext
from datetime import datetime
from collections import deque
from itertools import count
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from inventorylite import utils, dates, assembly_resolver
from inventorylite.text_norm import norm_text
from inventorylite.utils import get_db_path


LATEST_SCHEMA_VERSION = 5


def _strip_weird(value: str | None) -> str:
    return (value or "").replace("\u200b", "").replace("\ufeff", "").replace("\u00a0", " ")


def normalize_sku(raw: str) -> str:
    cleaned = _strip_weird(raw).strip()
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = cleaned.upper()
    if not cleaned:
        raise ValueError("SKU порожній/некоректний")
    return cleaned


def normalize_barcode(raw: str) -> str:
    cleaned = _strip_weird(raw).strip()
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = cleaned.upper()
    if not cleaned:
        raise ValueError("Штрихкод порожній/некоректний")
    return cleaned


def normalize_supplier_sku(raw: str | None) -> str | None:
    cleaned = _strip_weird(raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return None
    return cleaned


def normalize_external_sku(raw: str) -> str:
    cleaned = _strip_weird(raw).strip()
    if not cleaned:
        raise ValueError("Зовнішній SKU порожній/некоректний")
    return cleaned


def _normalize_date(raw: str, field_label: str = "Дата") -> str:
    return dates.normalize_date_to_iso(raw, field_label=field_label)


def _normalize_optional_date(raw: str | None, field_label: str = "Дата") -> str | None:
    return dates.normalize_optional_date_to_iso(raw, field_label=field_label)


def get_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    apply_connection_pragmas(conn)
    return conn


def apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA temp_store = MEMORY")


def db_health_check() -> dict:
    conn = get_connection()
    try:
        integrity_row = conn.execute("PRAGMA integrity_check;").fetchone()
        integrity_value = integrity_row[0] if integrity_row else "unknown"
        foreign_key_rows = conn.execute("PRAGMA foreign_key_check;").fetchall()
        foreign_key_issues = len(foreign_key_rows)

        counts = {
            "Products": int(conn.execute("SELECT COUNT(*) FROM Products").fetchone()[0]),
            "PurchaseDocuments": int(
                conn.execute("SELECT COUNT(*) FROM PurchaseDocuments").fetchone()[0]
            ),
            "SalesDocuments": int(conn.execute("SELECT COUNT(*) FROM SalesDocuments").fetchone()[0]),
            "StockBalances": int(conn.execute("SELECT COUNT(*) FROM StockBalances").fetchone()[0]),
            "CashTransactions": int(conn.execute("SELECT COUNT(*) FROM CashTransactions").fetchone()[0]),
        }
    finally:
        conn.close()

    return {
        "integrity_check": "ok" if integrity_value == "ok" else str(integrity_value),
        "foreign_key_issues": foreign_key_issues,
        "counts": counts,
    }


def db_quick_repair() -> dict:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(get_db_path(), timeout=5.0)
        conn.isolation_level = None
        apply_connection_pragmas(conn)
        conn.execute("PRAGMA optimize;")
        conn.execute("REINDEX;")
        conn.execute("VACUUM;")
        return {"ok": True}
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if "locked" in message.lower():
            return {"ok": False, "error": "База даних зайнята. Закрийте інші вікна та повторіть."}
        return {"ok": False, "error": message}
    except Exception as exc:  # pragma: no cover - defensive path
        return {"ok": False, "error": str(exc)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterable[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


_SAVEPOINT_COUNTER = count(1)


@contextmanager
def safe_transaction(conn: sqlite3.Connection) -> Iterable[None]:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        return

    savepoint = f"sp_{next(_SAVEPOINT_COUNTER)}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")


def _get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_user_version(conn: sqlite3.Connection, v: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(v)}")


def _audit_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='AuditLog'").fetchone()
    return row is not None


def audit_event(
    event_type: str,
    message: str,
    *,
    level: str = "INFO",
    details: dict | None = None,
    related_doc_type: str | None = None,
    related_doc_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """
    Пише подію в AuditLog.
    Гарантія: не кидає exception назовні. Якщо не може записати — тільки logging.warning.
    Якщо conn передали і він у транзакції — вставка робиться через SAVEPOINT і не ламає основний транзакційний блок.
    """

    try:
        if conn is None:
            with get_connection() as new_conn:
                audit_event(
                    event_type,
                    message,
                    level=level,
                    details=details,
                    related_doc_type=related_doc_type,
                    related_doc_id=related_doc_id,
                    conn=new_conn,
                )
            return

        if not _audit_table_exists(conn):
            return

        details_json = json.dumps(details, ensure_ascii=False) if details else None
        actor = os.getenv("USERNAME") or os.getenv("USER") or None
        host = platform.node() or None
        pid = os.getpid()
        params = (
            event_type,
            level,
            message,
            details_json,
            related_doc_type,
            related_doc_id,
            actor,
            host,
            pid,
        )

        if conn.in_transaction:
            savepoint = f"audit_{next(_SAVEPOINT_COUNTER)}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                conn.execute(
                    "INSERT INTO AuditLog (event_type, level, message, details_json, related_doc_type, related_doc_id, actor, host, pid) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    params,
                )
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                try:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                except Exception:
                    logging.debug("Failed to rollback audit savepoint", exc_info=True)
                logging.warning("Failed to write audit event (transactional)", exc_info=True)
            return

        try:
            with safe_transaction(conn):
                conn.execute(
                    "INSERT INTO AuditLog (event_type, level, message, details_json, related_doc_type, related_doc_id, actor, host, pid) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    params,
                )
        except Exception:
            logging.warning("Failed to write audit event", exc_info=True)
    except Exception:
        logging.warning("Audit event logging encountered an unexpected error", exc_info=True)


def init_db() -> None:
    """Create database schema if it does not exist."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS Brands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS Categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                parent_id INTEGER,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_service INTEGER NOT NULL DEFAULT 0,
                is_hidden INTEGER NOT NULL DEFAULT 0,
                color TEXT,
                icon TEXT,
                typical_attributes TEXT,
                FOREIGN KEY(parent_id) REFERENCES Categories(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS Warehouses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS SalesChannels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS Currencies (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                decimals INTEGER NOT NULL DEFAULT 2,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS CurrencyRates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                currency_code TEXT NOT NULL,
                rate_date TEXT NOT NULL,
                rate REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (currency_code) REFERENCES Currencies(code) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS Products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT UNIQUE NOT NULL,
                supplier_sku TEXT,
                name TEXT UNIQUE NOT NULL,
                unit TEXT NOT NULL DEFAULT 'pcs',
                brand_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (brand_id) REFERENCES Brands(id) ON DELETE CASCADE,
                FOREIGN KEY (category_id) REFERENCES Categories(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ProductSupplierCodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                supplier_id INTEGER NOT NULL,
                supplier_sku TEXT NOT NULL,
                is_primary INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
                FOREIGN KEY (supplier_id) REFERENCES Counterparties(id) ON DELETE CASCADE,
                UNIQUE (supplier_id, supplier_sku)
            );
            CREATE INDEX IF NOT EXISTS idx_psc_supplier_sku_lower
                ON ProductSupplierCodes(supplier_id, lower(trim(supplier_sku)));
            CREATE INDEX IF NOT EXISTS idx_psc_product
                ON ProductSupplierCodes(product_id);

            CREATE TABLE IF NOT EXISTS ProductChannelCodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                external_sku TEXT NOT NULL,
                external_name TEXT,
                is_primary INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_seen_at TEXT,
                note TEXT,
                FOREIGN KEY (channel_id) REFERENCES SalesChannels(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pcc_channel_sku
                ON ProductChannelCodes(channel_id, lower(trim(external_sku)));
            CREATE INDEX IF NOT EXISTS idx_pcc_product ON ProductChannelCodes(product_id);
            CREATE INDEX IF NOT EXISTS idx_pcc_channel ON ProductChannelCodes(channel_id);
            CREATE INDEX IF NOT EXISTS idx_pcc_active ON ProductChannelCodes(is_active);

            CREATE TABLE IF NOT EXISTS ProductBarcodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                note TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
                UNIQUE(code)
            );
            CREATE INDEX IF NOT EXISTS idx_pb_product_id ON ProductBarcodes(product_id);
            CREATE INDEX IF NOT EXISTS idx_pb_code_lower ON ProductBarcodes(lower(trim(code)));

            CREATE TABLE IF NOT EXISTS ProductImages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                original_name TEXT,
                sort_order INTEGER NOT NULL,
                is_primary INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_pi_product_id ON ProductImages(product_id);
            CREATE INDEX IF NOT EXISTS idx_pi_product_sort ON ProductImages(product_id, sort_order);
            CREATE INDEX IF NOT EXISTS idx_pi_product_primary ON ProductImages(product_id, is_primary);

            CREATE TABLE IF NOT EXISTS AdditionalProductCategories (
                product_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                PRIMARY KEY (product_id, category_id),
                FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
                FOREIGN KEY (category_id) REFERENCES Categories(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ComponentGroups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS AssemblySlots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS ProductComponentGroupMembers (
                group_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                PRIMARY KEY (group_id, product_id),
                FOREIGN KEY (group_id) REFERENCES ComponentGroups(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ProductSlotCoverage (
                product_id INTEGER NOT NULL,
                slot_id INTEGER NOT NULL,
                PRIMARY KEY (product_id, slot_id),
                FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
                FOREIGN KEY (slot_id) REFERENCES AssemblySlots(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS ProductAssemblyRequirements (
                product_id INTEGER NOT NULL,
                slot_id INTEGER NOT NULL,
                group_id INTEGER,
                qty REAL NOT NULL DEFAULT 1,
                PRIMARY KEY (product_id, slot_id),
                FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
                FOREIGN KEY (slot_id) REFERENCES AssemblySlots(id) ON DELETE CASCADE,
                FOREIGN KEY (group_id) REFERENCES ComponentGroups(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_component_groups_code_lower ON ComponentGroups(lower(trim(code)));
            CREATE INDEX IF NOT EXISTS idx_assembly_slots_code_lower ON AssemblySlots(lower(trim(code)));
            CREATE INDEX IF NOT EXISTS idx_pcm_product ON ProductComponentGroupMembers(product_id);
            CREATE INDEX IF NOT EXISTS idx_pcm_group ON ProductComponentGroupMembers(group_id);
            CREATE INDEX IF NOT EXISTS idx_slotcov_product_id ON ProductSlotCoverage(product_id);
            CREATE INDEX IF NOT EXISTS idx_slotcov_slot_id ON ProductSlotCoverage(slot_id);
            CREATE INDEX IF NOT EXISTS idx_par_product ON ProductAssemblyRequirements(product_id);
            CREATE INDEX IF NOT EXISTS idx_par_slot ON ProductAssemblyRequirements(slot_id);
            CREATE INDEX IF NOT EXISTS idx_par_group ON ProductAssemblyRequirements(group_id);

            CREATE INDEX IF NOT EXISTS idx_products_sku_lower ON Products(lower(trim(sku)));
            CREATE INDEX IF NOT EXISTS idx_products_name_lower ON Products(lower(name));
            CREATE INDEX IF NOT EXISTS idx_products_supplier_sku_lower ON Products(lower(trim(supplier_sku)));

            CREATE TABLE IF NOT EXISTS ProductCategoryLinks (
                product_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                PRIMARY KEY (product_id, category_id),
                FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
                FOREIGN KEY (category_id) REFERENCES Categories(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS Counterparties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('supplier','customer','both','other')),
                phone TEXT,
                email TEXT,
                address TEXT,
                note TEXT,
                UNIQUE(name, type)
            );

            CREATE TABLE IF NOT EXISTS PurchaseDocuments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_date TEXT NOT NULL,
                supplier_id INTEGER,
                warehouse_id INTEGER NOT NULL,
                channel TEXT,
                status TEXT NOT NULL CHECK(status IN ('draft','posted')) DEFAULT 'draft',
                comment TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (supplier_id) REFERENCES Counterparties(id),
                FOREIGN KEY (warehouse_id) REFERENCES Warehouses(id)
            );

            CREATE TABLE IF NOT EXISTS PurchaseLines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity REAL NOT NULL,
                purchase_price REAL NOT NULL,
                amount REAL NOT NULL,
                FOREIGN KEY (purchase_id) REFERENCES PurchaseDocuments(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES Products(id)
            );
            CREATE INDEX IF NOT EXISTS idx_purchase_lines_purchase ON PurchaseLines(purchase_id);

            CREATE TABLE IF NOT EXISTS SalesDocuments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_date TEXT NOT NULL,
                customer_id INTEGER,
                warehouse_id INTEGER NOT NULL,
                channel TEXT,
                status TEXT NOT NULL CHECK(status IN ('draft','posted')) DEFAULT 'draft',
                comment TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                currency_code TEXT NOT NULL DEFAULT 'UAH',
                exchange_rate REAL NOT NULL DEFAULT 1,
                order_expense_doc REAL NOT NULL DEFAULT 0,
                order_expense_base REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (customer_id) REFERENCES Counterparties(id),
                FOREIGN KEY (warehouse_id) REFERENCES Warehouses(id)
            );

            CREATE TABLE IF NOT EXISTS SalesLines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity REAL NOT NULL,
                sale_price REAL NOT NULL,
                amount REAL NOT NULL,
                amount_doc REAL NOT NULL DEFAULT 0,
                sale_price_base REAL NOT NULL DEFAULT 0,
                unit_expense_doc REAL NOT NULL DEFAULT 0,
                unit_expense_base REAL NOT NULL DEFAULT 0,
                order_expense_allocated_base REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (sale_id) REFERENCES SalesDocuments(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES Products(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sales_lines_sale ON SalesLines(sale_id);

            CREATE TABLE IF NOT EXISTS SaleAssemblyPlans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL,
                sale_line_id INTEGER NOT NULL UNIQUE,
                parent_product_id INTEGER NOT NULL,
                warehouse_id INTEGER NOT NULL,
                parent_qty REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sale_id) REFERENCES SalesDocuments(id) ON DELETE CASCADE,
                FOREIGN KEY (sale_line_id) REFERENCES SalesLines(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_product_id) REFERENCES Products(id),
                FOREIGN KEY (warehouse_id) REFERENCES Warehouses(id)
            );
            CREATE TABLE IF NOT EXISTS SaleAssemblyPlanLines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                component_product_id INTEGER NOT NULL,
                qty REAL NOT NULL,
                cost_per_unit REAL NOT NULL,
                amount REAL NOT NULL,
                details_json TEXT,
                FOREIGN KEY (plan_id) REFERENCES SaleAssemblyPlans(id) ON DELETE CASCADE,
                FOREIGN KEY (component_product_id) REFERENCES Products(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sapl_plan ON SaleAssemblyPlanLines(plan_id);
            CREATE INDEX IF NOT EXISTS idx_sapl_component ON SaleAssemblyPlanLines(component_product_id);
            CREATE INDEX IF NOT EXISTS idx_sap_sale_id ON SaleAssemblyPlans(sale_id);

            CREATE TABLE IF NOT EXISTS InventoryDocuments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_date TEXT NOT NULL,
                warehouse_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('draft','posted')) DEFAULT 'draft',
                comment TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (warehouse_id) REFERENCES Warehouses(id)
            );
            CREATE INDEX IF NOT EXISTS idx_inventory_docs_date ON InventoryDocuments(doc_date);
            CREATE INDEX IF NOT EXISTS idx_inventory_docs_wh ON InventoryDocuments(warehouse_id);

            CREATE TABLE IF NOT EXISTS InventoryLines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inventory_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                expected_qty REAL NOT NULL DEFAULT 0,
                counted_qty REAL NOT NULL DEFAULT 0,
                cost_override REAL,
                note TEXT,
                UNIQUE(inventory_id, product_id),
                FOREIGN KEY (inventory_id) REFERENCES InventoryDocuments(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES Products(id)
            );
            CREATE INDEX IF NOT EXISTS idx_inventory_lines_inv ON InventoryLines(inventory_id);

            CREATE TABLE IF NOT EXISTS StockBalances (
                product_id INTEGER NOT NULL,
                warehouse_id INTEGER NOT NULL,
                quantity REAL NOT NULL DEFAULT 0,
                average_cost REAL NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (product_id, warehouse_id),
                FOREIGN KEY (product_id) REFERENCES Products(id),
                FOREIGN KEY (warehouse_id) REFERENCES Warehouses(id)
            );

            CREATE TABLE IF NOT EXISTS StockMoves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                move_date TEXT NOT NULL,
                product_id INTEGER NOT NULL,
                warehouse_id INTEGER NOT NULL,
                qty_in REAL NOT NULL DEFAULT 0,
                qty_out REAL NOT NULL DEFAULT 0,
                cost_per_unit REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                reference_type TEXT NOT NULL,
                reference_id INTEGER NOT NULL,
                channel TEXT,
                counterparty_id INTEGER,
                FOREIGN KEY (product_id) REFERENCES Products(id),
                FOREIGN KEY (warehouse_id) REFERENCES Warehouses(id)
            );
            CREATE INDEX IF NOT EXISTS idx_stock_moves_ref ON StockMoves(reference_type, reference_id);

            CREATE TABLE IF NOT EXISTS CashTransactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL,
                type TEXT NOT NULL CHECK(type IN (
                    'sale_payment','purchase_payment','other_income','other_variable_expense','fixed_expense'
                )),
                counterparty_id INTEGER,
                related_doc_type TEXT,
                related_doc_id INTEGER,
                channel TEXT,
                comment TEXT,
                currency_code TEXT NOT NULL DEFAULT 'UAH',
                exchange_rate REAL NOT NULL DEFAULT 1,
                amount_doc REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(counterparty_id) REFERENCES Counterparties(id)
            );
            CREATE INDEX IF NOT EXISTS idx_cash_date ON CashTransactions(date);

            CREATE TABLE IF NOT EXISTS ExtraCostDocuments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_date TEXT NOT NULL,
                currency_code TEXT NOT NULL DEFAULT 'UAH',
                exchange_rate REAL NOT NULL DEFAULT 1,
                partner_id INTEGER,
                status TEXT NOT NULL CHECK(status IN ('draft','posted')) DEFAULT 'draft',
                total_amount_doc REAL NOT NULL DEFAULT 0,
                total_amount_base REAL NOT NULL DEFAULT 0,
                comment TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (partner_id) REFERENCES Counterparties(id)
            );

            CREATE TABLE IF NOT EXISTS ExtraCostLines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                extra_cost_id INTEGER NOT NULL,
                cost_type TEXT NOT NULL,
                amount_doc REAL NOT NULL,
                amount_base REAL NOT NULL,
                FOREIGN KEY (extra_cost_id) REFERENCES ExtraCostDocuments(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_extra_cost_lines_doc ON ExtraCostLines(extra_cost_id);

            CREATE TABLE IF NOT EXISTS ExtraCostAllocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                extra_cost_id INTEGER NOT NULL,
                purchase_line_id INTEGER NOT NULL,
                amount_allocated_base REAL NOT NULL,
                FOREIGN KEY (extra_cost_id) REFERENCES ExtraCostDocuments(id) ON DELETE CASCADE,
                FOREIGN KEY (purchase_line_id) REFERENCES PurchaseLines(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_extra_cost_allocations_doc ON ExtraCostAllocations(extra_cost_id);

            CREATE TABLE IF NOT EXISTS ExtraCostCogsAllocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                extra_cost_id INTEGER NOT NULL,
                purchase_line_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                warehouse_id INTEGER NOT NULL,
                qty_sold REAL NOT NULL,
                amount_cogs_base REAL NOT NULL,
                FOREIGN KEY (extra_cost_id) REFERENCES ExtraCostDocuments(id) ON DELETE CASCADE,
                FOREIGN KEY (purchase_line_id) REFERENCES PurchaseLines(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_ecca_doc ON ExtraCostCogsAllocations(extra_cost_id);
            CREATE INDEX IF NOT EXISTS idx_ecca_product ON ExtraCostCogsAllocations(product_id);

            CREATE TABLE IF NOT EXISTS LabelTemplates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                page_w_mm REAL NOT NULL,
                page_h_mm REAL NOT NULL,
                orientation TEXT NOT NULL DEFAULT 'portrait',
                cols INTEGER NOT NULL,
                rows INTEGER NOT NULL,
                label_w_mm REAL NOT NULL,
                label_h_mm REAL NOT NULL,
                gap_x_mm REAL NOT NULL DEFAULT 0,
                gap_y_mm REAL NOT NULL DEFAULT 0,
                margin_left_mm REAL NOT NULL DEFAULT 0,
                margin_top_mm REAL NOT NULL DEFAULT 0,
                margin_right_mm REAL NOT NULL DEFAULT 0,
                margin_bottom_mm REAL NOT NULL DEFAULT 0,
                offset_x_mm REAL NOT NULL DEFAULT 0,
                offset_y_mm REAL NOT NULL DEFAULT 0,
                scale_x REAL NOT NULL DEFAULT 1.0,
                scale_y REAL NOT NULL DEFAULT 1.0,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0,
                schema_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_label_templates_active ON LabelTemplates(is_active);

            CREATE TABLE IF NOT EXISTS LabelTemplateElements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL REFERENCES LabelTemplates(id) ON DELETE CASCADE,
                element_type TEXT NOT NULL,
                field_key TEXT,
                x_mm REAL NOT NULL,
                y_mm REAL NOT NULL,
                w_mm REAL NOT NULL,
                h_mm REAL NOT NULL,
                rotation_deg REAL NOT NULL DEFAULT 0,
                align TEXT NOT NULL DEFAULT 'left',
                font_name TEXT NOT NULL DEFAULT 'Helvetica',
                font_size REAL NOT NULL DEFAULT 9,
                max_chars INTEGER,
                wrap INTEGER NOT NULL DEFAULT 0,
                options_json TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (template_id) REFERENCES LabelTemplates(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_label_elements_template_sort ON LabelTemplateElements(template_id, sort_order);
            """
        )
        _apply_migrations(conn)
        _migrate_schema(conn, commit=True)
        final = _get_user_version(conn)
        if final != LATEST_SCHEMA_VERSION:
            raise ValueError(f"Schema version mismatch: db={final}, app={LATEST_SCHEMA_VERSION}")
    logging.info("Database initialized at %s", db_path)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    current = _get_user_version(conn)
    if current > LATEST_SCHEMA_VERSION:
        raise ValueError(
            f"База даних новішої версії (schema v{current}), ніж ця програма (v{LATEST_SCHEMA_VERSION}). "
            "Оновіть програму або відновіть стару копію БД."
        )

    if current == LATEST_SCHEMA_VERSION:
        return

    if current != 0:
        try:
            utils.backup_database(get_db_path(), conn=conn)
        except Exception:
            logging.exception("Pre-migration backup failed (continuing)")

    for v in range(current + 1, LATEST_SCHEMA_VERSION + 1):
        logging.info("Applying DB migration v%s", v)
        with safe_transaction(conn):
            if v == 1:
                _migration_v1_baseline(conn)
            elif v == 2:
                _migration_v2_audit_log(conn)
            elif v == 3:
                _migration_v3_assembly_requirements(conn)
            elif v == 4:
                _migration_v4_sale_assembly(conn)
            elif v == 5:
                _migration_v5_product_images(conn)
            else:
                raise RuntimeError(f"Unknown migration step: {v}")
            _set_user_version(conn, v)
            try:
                audit_event(
                    "DB_MIGRATION",
                    f"Applied DB migration v{v}",
                    details={"from_version": v - 1, "to_version": v},
                    conn=conn,
                )
            except Exception:
                logging.warning("Audit logging for migration v%s failed", v, exc_info=True)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if not _column_exists(conn, table, column):
        logging.info("Adding missing column %s.%s", table, column)
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migration_v2_audit_log(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS AuditLog(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          event_type TEXT NOT NULL,
          level TEXT NOT NULL DEFAULT 'INFO',
          message TEXT NOT NULL,
          details_json TEXT,
          related_doc_type TEXT,
          related_doc_id INTEGER,
          actor TEXT,
          host TEXT,
          pid INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_audit_created_at ON AuditLog(created_at);
        CREATE INDEX IF NOT EXISTS idx_audit_event_type ON AuditLog(event_type);
        CREATE INDEX IF NOT EXISTS idx_audit_related ON AuditLog(related_doc_type, related_doc_id);
        """
    )


def _migration_v3_assembly_requirements(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_psc_product;
        DROP INDEX IF EXISTS idx_psc_slot;
        CREATE INDEX IF NOT EXISTS idx_slotcov_product_id ON ProductSlotCoverage(product_id);
        CREATE INDEX IF NOT EXISTS idx_slotcov_slot_id ON ProductSlotCoverage(slot_id);

        CREATE TABLE IF NOT EXISTS ProductAssemblyRequirements (
          product_id INTEGER NOT NULL,
          slot_id INTEGER NOT NULL,
          group_id INTEGER,
          qty REAL NOT NULL DEFAULT 1,
          PRIMARY KEY (product_id, slot_id),
          FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
          FOREIGN KEY (slot_id) REFERENCES AssemblySlots(id) ON DELETE CASCADE,
          FOREIGN KEY (group_id) REFERENCES ComponentGroups(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_par_product ON ProductAssemblyRequirements(product_id);
        CREATE INDEX IF NOT EXISTS idx_par_slot ON ProductAssemblyRequirements(slot_id);
        CREATE INDEX IF NOT EXISTS idx_par_group ON ProductAssemblyRequirements(group_id);
        """
    )


def _migration_v4_sale_assembly(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS SaleAssemblyPlans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            sale_line_id INTEGER NOT NULL UNIQUE,
            parent_product_id INTEGER NOT NULL,
            warehouse_id INTEGER NOT NULL,
            parent_qty REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sale_id) REFERENCES SalesDocuments(id) ON DELETE CASCADE,
            FOREIGN KEY (sale_line_id) REFERENCES SalesLines(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_product_id) REFERENCES Products(id),
            FOREIGN KEY (warehouse_id) REFERENCES Warehouses(id)
        );
        CREATE TABLE IF NOT EXISTS SaleAssemblyPlanLines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            component_product_id INTEGER NOT NULL,
            qty REAL NOT NULL,
            cost_per_unit REAL NOT NULL,
            amount REAL NOT NULL,
            details_json TEXT,
            FOREIGN KEY (plan_id) REFERENCES SaleAssemblyPlans(id) ON DELETE CASCADE,
            FOREIGN KEY (component_product_id) REFERENCES Products(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sapl_plan ON SaleAssemblyPlanLines(plan_id);
        CREATE INDEX IF NOT EXISTS idx_sapl_component ON SaleAssemblyPlanLines(component_product_id);
        CREATE INDEX IF NOT EXISTS idx_sap_sale_id ON SaleAssemblyPlans(sale_id);
        """
    )


def _migration_v5_product_images(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ProductImages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL,
            original_name TEXT,
            sort_order INTEGER NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pi_product_id ON ProductImages(product_id);
        CREATE INDEX IF NOT EXISTS idx_pi_product_sort ON ProductImages(product_id, sort_order);
        CREATE INDEX IF NOT EXISTS idx_pi_product_primary ON ProductImages(product_id, is_primary);
        """
    )


def _migration_v1_baseline(conn: sqlite3.Connection) -> None:
    _migrate_schema(conn, commit=False)
    _normalize_existing_codes(conn, use_transaction=False)
    _ensure_case_insensitive_uniques(conn, use_transaction=False)
    _ensure_label_templates(conn)


def _migrate_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    """Ensure legacy databases get new columns required by current version."""

    _ensure_column(conn, "Products", "unit", "TEXT NOT NULL DEFAULT 'pcs'")
    _ensure_column(conn, "Products", "is_active", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "Products", "supplier_sku", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_supplier_sku_lower ON Products(lower(trim(supplier_sku)))"
    )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ProductSupplierCodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            supplier_id INTEGER NOT NULL,
            supplier_sku TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
            FOREIGN KEY (supplier_id) REFERENCES Counterparties(id) ON DELETE CASCADE,
            UNIQUE (supplier_id, supplier_sku)
        );
        CREATE INDEX IF NOT EXISTS idx_psc_supplier_sku_lower
            ON ProductSupplierCodes(supplier_id, lower(trim(supplier_sku)));
        CREATE INDEX IF NOT EXISTS idx_psc_product
            ON ProductSupplierCodes(product_id);

        CREATE TABLE IF NOT EXISTS ProductChannelCodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            external_sku TEXT NOT NULL,
            external_name TEXT,
            is_primary INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_seen_at TEXT,
            note TEXT,
            FOREIGN KEY (channel_id) REFERENCES SalesChannels(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pcc_channel_sku
            ON ProductChannelCodes(channel_id, lower(trim(external_sku)));
        CREATE INDEX IF NOT EXISTS idx_pcc_product ON ProductChannelCodes(product_id);
        CREATE INDEX IF NOT EXISTS idx_pcc_channel ON ProductChannelCodes(channel_id);
        CREATE INDEX IF NOT EXISTS idx_pcc_active ON ProductChannelCodes(is_active);

        CREATE TABLE IF NOT EXISTS ProductBarcodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
            UNIQUE(code)
        );
        CREATE INDEX IF NOT EXISTS idx_pb_product_id ON ProductBarcodes(product_id);
        CREATE INDEX IF NOT EXISTS idx_pb_code_lower ON ProductBarcodes(lower(trim(code)));

        CREATE TABLE IF NOT EXISTS ProductImages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL,
            original_name TEXT,
            sort_order INTEGER NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pi_product_id ON ProductImages(product_id);
        CREATE INDEX IF NOT EXISTS idx_pi_product_sort ON ProductImages(product_id, sort_order);
        CREATE INDEX IF NOT EXISTS idx_pi_product_primary ON ProductImages(product_id, is_primary);

        CREATE TABLE IF NOT EXISTS InventoryDocuments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_date TEXT NOT NULL,
            warehouse_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft','posted')) DEFAULT 'draft',
            comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (warehouse_id) REFERENCES Warehouses(id)
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_docs_date ON InventoryDocuments(doc_date);
        CREATE INDEX IF NOT EXISTS idx_inventory_docs_wh ON InventoryDocuments(warehouse_id);

        CREATE TABLE IF NOT EXISTS InventoryLines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            expected_qty REAL NOT NULL DEFAULT 0,
            counted_qty REAL NOT NULL DEFAULT 0,
            cost_override REAL,
            note TEXT,
            UNIQUE(inventory_id, product_id),
            FOREIGN KEY (inventory_id) REFERENCES InventoryDocuments(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES Products(id)
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_lines_inv ON InventoryLines(inventory_id);
        """
    )

    _ensure_column(conn, "Categories", "parent_id", "INTEGER REFERENCES Categories(id) ON DELETE SET NULL")
    _ensure_column(conn, "Categories", "sort_order", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "Categories", "is_service", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "Categories", "is_hidden", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "Categories", "color", "TEXT")
    _ensure_column(conn, "Categories", "icon", "TEXT")
    _ensure_column(conn, "Categories", "typical_attributes", "TEXT")

    _ensure_column(conn, "Counterparties", "type", "TEXT NOT NULL DEFAULT 'other'")
    _ensure_column(conn, "Counterparties", "phone", "TEXT")
    _ensure_column(conn, "Counterparties", "email", "TEXT")
    _ensure_column(conn, "Counterparties", "address", "TEXT")
    _ensure_column(conn, "Counterparties", "note", "TEXT")

    _ensure_column(conn, "Warehouses", "description", "TEXT")
    _ensure_column(conn, "Warehouses", "is_active", "INTEGER NOT NULL DEFAULT 1")

    _ensure_column(conn, "SalesChannels", "is_active", "INTEGER NOT NULL DEFAULT 1")

    _ensure_column(conn, "PurchaseDocuments", "channel", "TEXT")
    _ensure_column(conn, "PurchaseDocuments", "status", "TEXT NOT NULL DEFAULT 'draft'")
    _ensure_column(conn, "PurchaseDocuments", "comment", "TEXT")
    _ensure_column(conn, "PurchaseDocuments", "created_at", "TEXT DEFAULT CURRENT_TIMESTAMP")
    _ensure_column(conn, "PurchaseDocuments", "currency_code", "TEXT NOT NULL DEFAULT 'UAH'")
    _ensure_column(conn, "PurchaseDocuments", "exchange_rate", "REAL NOT NULL DEFAULT 1")

    _ensure_column(conn, "PurchaseLines", "amount", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "PurchaseLines", "amount_doc", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "PurchaseLines", "purchase_price_base", "REAL NOT NULL DEFAULT 0")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS LabelTemplates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            kind TEXT NOT NULL,
            page_w_mm REAL NOT NULL,
            page_h_mm REAL NOT NULL,
            orientation TEXT NOT NULL DEFAULT 'portrait',
            cols INTEGER NOT NULL,
            rows INTEGER NOT NULL,
            label_w_mm REAL NOT NULL,
            label_h_mm REAL NOT NULL,
            gap_x_mm REAL NOT NULL DEFAULT 0,
            gap_y_mm REAL NOT NULL DEFAULT 0,
            margin_left_mm REAL NOT NULL DEFAULT 0,
            margin_top_mm REAL NOT NULL DEFAULT 0,
            margin_right_mm REAL NOT NULL DEFAULT 0,
            margin_bottom_mm REAL NOT NULL DEFAULT 0,
            offset_x_mm REAL NOT NULL DEFAULT 0,
            offset_y_mm REAL NOT NULL DEFAULT 0,
            scale_x REAL NOT NULL DEFAULT 1.0,
            scale_y REAL NOT NULL DEFAULT 1.0,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_default INTEGER NOT NULL DEFAULT 0,
            schema_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_label_templates_active ON LabelTemplates(is_active);

        CREATE TABLE IF NOT EXISTS LabelTemplateElements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL REFERENCES LabelTemplates(id) ON DELETE CASCADE,
            element_type TEXT NOT NULL,
            field_key TEXT,
            x_mm REAL NOT NULL,
            y_mm REAL NOT NULL,
            w_mm REAL NOT NULL,
            h_mm REAL NOT NULL,
            rotation_deg REAL NOT NULL DEFAULT 0,
            align TEXT NOT NULL DEFAULT 'left',
            font_name TEXT NOT NULL DEFAULT 'Helvetica',
            font_size REAL NOT NULL DEFAULT 9,
            max_chars INTEGER,
            wrap INTEGER NOT NULL DEFAULT 0,
            options_json TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (template_id) REFERENCES LabelTemplates(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_label_elements_template_sort ON LabelTemplateElements(template_id, sort_order);
        """
    )

    _ensure_column(conn, "PurchaseLines", "extra_cost_allocated_base", "REAL NOT NULL DEFAULT 0")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS AdditionalProductCategories (
            product_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            PRIMARY KEY (product_id, category_id),
            FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
            FOREIGN KEY (category_id) REFERENCES Categories(id) ON DELETE CASCADE
        )
        """
    )

    _ensure_column(conn, "SalesDocuments", "channel", "TEXT")
    _ensure_column(conn, "SalesDocuments", "status", "TEXT NOT NULL DEFAULT 'draft'")
    _ensure_column(conn, "SalesDocuments", "comment", "TEXT")
    _ensure_column(conn, "SalesDocuments", "created_at", "TEXT DEFAULT CURRENT_TIMESTAMP")
    _ensure_column(conn, "SalesDocuments", "currency_code", "TEXT NOT NULL DEFAULT 'UAH'")
    _ensure_column(conn, "SalesDocuments", "exchange_rate", "REAL NOT NULL DEFAULT 1")
    _ensure_column(conn, "SalesDocuments", "order_expense_doc", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "SalesDocuments", "order_expense_base", "REAL NOT NULL DEFAULT 0")

    _ensure_column(conn, "SalesLines", "amount", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "SalesLines", "amount_doc", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "SalesLines", "sale_price_base", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "SalesLines", "unit_expense_doc", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "SalesLines", "unit_expense_base", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "SalesLines", "order_expense_allocated_base", "REAL NOT NULL DEFAULT 0")

    _ensure_column(conn, "StockMoves", "channel", "TEXT")
    _ensure_column(conn, "StockMoves", "counterparty_id", "INTEGER")

    _ensure_column(conn, "CashTransactions", "type", "TEXT NOT NULL DEFAULT 'other_income'")
    _ensure_column(conn, "CashTransactions", "counterparty_id", "INTEGER")
    _ensure_column(conn, "CashTransactions", "currency_code", "TEXT NOT NULL DEFAULT 'UAH'")
    _ensure_column(conn, "CashTransactions", "exchange_rate", "REAL NOT NULL DEFAULT 1")
    _ensure_column(conn, "CashTransactions", "amount_doc", "REAL NOT NULL DEFAULT 0")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ExtraCostCogsAllocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            extra_cost_id INTEGER NOT NULL,
            purchase_line_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            warehouse_id INTEGER NOT NULL,
            qty_sold REAL NOT NULL,
            amount_cogs_base REAL NOT NULL,
            FOREIGN KEY (extra_cost_id) REFERENCES ExtraCostDocuments(id) ON DELETE CASCADE,
            FOREIGN KEY (purchase_line_id) REFERENCES PurchaseLines(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_ecca_doc ON ExtraCostCogsAllocations(extra_cost_id);
        CREATE INDEX IF NOT EXISTS idx_ecca_product ON ExtraCostCogsAllocations(product_id);
        """
    )

    if commit:
        conn.commit()


def _normalize_existing_codes(conn: sqlite3.Connection, *, use_transaction: bool = True) -> None:
    product_rows = conn.execute("SELECT id, sku, supplier_sku FROM Products").fetchall()
    barcode_rows = conn.execute("SELECT id, code FROM ProductBarcodes").fetchall()
    supplier_rows = conn.execute(
        "SELECT id, supplier_id, supplier_sku FROM ProductSupplierCodes"
    ).fetchall()

    conflicts: list[str] = []
    sku_seen: dict[str, tuple[int, str]] = {}
    barcode_seen: dict[str, tuple[int, str]] = {}
    supplier_seen: dict[tuple[int, str], tuple[int, str]] = {}

    product_updates: list[tuple[str, str | None, int]] = []
    barcode_updates: list[tuple[str, int]] = []
    supplier_updates: list[tuple[str, int]] = []

    for row in product_rows:
        try:
            normalized_sku = normalize_sku(row["sku"])
        except ValueError as exc:
            conflicts.append(f"Products id={row['id']} sku='{row['sku']}' -> {exc}")
            continue
        if normalized_sku in sku_seen and sku_seen[normalized_sku][0] != row["id"]:
            conflicts.append(
                "Products SKU conflict: "
                f"id={row['id']} sku='{row['sku']}' -> '{normalized_sku}' "
                f"collides with id={sku_seen[normalized_sku][0]} sku='{sku_seen[normalized_sku][1]}'"
            )
        else:
            sku_seen[normalized_sku] = (row["id"], row["sku"])
        supplier_sku = normalize_supplier_sku(row["supplier_sku"])
        if normalized_sku != row["sku"] or supplier_sku != row["supplier_sku"]:
            product_updates.append((normalized_sku, supplier_sku, row["id"]))

    for row in barcode_rows:
        try:
            normalized_code = normalize_barcode(row["code"])
        except ValueError as exc:
            conflicts.append(f"ProductBarcodes id={row['id']} code='{row['code']}' -> {exc}")
            continue
        if normalized_code in barcode_seen and barcode_seen[normalized_code][0] != row["id"]:
            conflicts.append(
                "ProductBarcodes conflict: "
                f"id={row['id']} code='{row['code']}' -> '{normalized_code}' "
                f"collides with id={barcode_seen[normalized_code][0]} code='{barcode_seen[normalized_code][1]}'"
            )
        else:
            barcode_seen[normalized_code] = (row["id"], row["code"])
        if normalized_code != row["code"]:
            barcode_updates.append((normalized_code, row["id"]))

    for row in supplier_rows:
        normalized_supplier = normalize_supplier_sku(row["supplier_sku"])
        if not normalized_supplier:
            conflicts.append(
                f"ProductSupplierCodes id={row['id']} supplier_id={row['supplier_id']} "
                f"supplier_sku='{row['supplier_sku']}' -> порожній/некоректний"
            )
            continue
        key = (row["supplier_id"], normalized_supplier.lower())
        if key in supplier_seen and supplier_seen[key][0] != row["id"]:
            conflicts.append(
                "ProductSupplierCodes conflict: "
                f"id={row['id']} supplier_id={row['supplier_id']} supplier_sku='{row['supplier_sku']}' "
                f"-> '{normalized_supplier}' collides with "
                f"id={supplier_seen[key][0]} supplier_sku='{supplier_seen[key][1]}'"
            )
        else:
            supplier_seen[key] = (row["id"], row["supplier_sku"])
        if normalized_supplier != row["supplier_sku"]:
            supplier_updates.append((normalized_supplier, row["id"]))

    if conflicts:
        raise ValueError(
            "Виявлено конфлікти під час нормалізації існуючих кодів:\n" + "\n".join(conflicts)
        )

    if not (product_updates or barcode_updates or supplier_updates):
        return

    context = safe_transaction(conn) if use_transaction else nullcontext()
    with context:
        if product_updates:
            conn.executemany(
                "UPDATE Products SET sku=?, supplier_sku=? WHERE id=?",
                product_updates,
            )
        if barcode_updates:
            conn.executemany("UPDATE ProductBarcodes SET code=? WHERE id=?", barcode_updates)
        if supplier_updates:
            conn.executemany(
                "UPDATE ProductSupplierCodes SET supplier_sku=? WHERE id=?", supplier_updates
            )


def _ensure_case_insensitive_uniques(conn: sqlite3.Connection, *, use_transaction: bool = True) -> None:
    conflicts: List[str] = []

    sku_rows = conn.execute(
        """
        SELECT lower(trim(sku)) AS key, group_concat(id || ':' || sku, ', ') AS items, COUNT(*) AS cnt
        FROM Products
        GROUP BY lower(trim(sku))
        HAVING cnt > 1
        """
    ).fetchall()
    if sku_rows:
        conflicts.append("Дублікати SKU (без урахування регістру):")
        conflicts.extend([f"  {row['key']}: {row['items']}" for row in sku_rows])

    barcode_rows = conn.execute(
        """
        SELECT lower(trim(code)) AS key, group_concat(id || ':' || code, ', ') AS items, COUNT(*) AS cnt
        FROM ProductBarcodes
        GROUP BY lower(trim(code))
        HAVING cnt > 1
        """
    ).fetchall()
    if barcode_rows:
        conflicts.append("Дублікати штрихкодів (без урахування регістру):")
        conflicts.extend([f"  {row['key']}: {row['items']}" for row in barcode_rows])

    supplier_rows = conn.execute(
        """
        SELECT supplier_id, lower(trim(supplier_sku)) AS key,
               group_concat(id || ':' || supplier_sku, ', ') AS items, COUNT(*) AS cnt
        FROM ProductSupplierCodes
        GROUP BY supplier_id, lower(trim(supplier_sku))
        HAVING cnt > 1
        """
    ).fetchall()
    if supplier_rows:
        conflicts.append("Дублікати кодів постачальника (без урахування регістру):")
        conflicts.extend(
            [f"  supplier_id={row['supplier_id']} {row['key']}: {row['items']}" for row in supplier_rows]
        )

    channel_rows = conn.execute(
        """
        SELECT channel_id, lower(trim(external_sku)) AS key,
               group_concat(id || ':' || external_sku, ', ') AS items, COUNT(*) AS cnt
        FROM ProductChannelCodes
        GROUP BY channel_id, lower(trim(external_sku))
        HAVING cnt > 1
        """
    ).fetchall()
    if channel_rows:
        conflicts.append("Дублікати кодів каналів (без урахування регістру):")
        conflicts.extend(
            [f"  channel_id={row['channel_id']} {row['key']}: {row['items']}" for row in channel_rows]
        )

    if conflicts:
        raise ValueError(
            "Знайдено конфлікти для унікальності без урахування регістру. "
            "Виправте дублікати вручну і перезапустіть програму.\n"
            + "\n".join(conflicts)
        )

    context = safe_transaction(conn) if use_transaction else nullcontext()
    with context:
        conn.execute("DROP INDEX IF EXISTS idx_products_sku_lower")
        conn.execute("DROP INDEX IF EXISTS idx_pb_code_lower")
        conn.execute("DROP INDEX IF EXISTS idx_psc_supplier_sku_lower")
        conn.execute("DROP INDEX IF EXISTS idx_pcc_channel_sku")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku_lower ON Products(lower(trim(sku)))"
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pb_code_lower ON ProductBarcodes(lower(trim(code)))")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_psc_supplier_sku_lower ON ProductSupplierCodes(supplier_id, lower(trim(supplier_sku)))"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pcc_channel_sku ON ProductChannelCodes(channel_id, lower(trim(external_sku)))"
        )


def _ensure_label_templates(conn: sqlite3.Connection) -> None:
    cur = conn.execute("SELECT COUNT(*) FROM LabelTemplates")
    if cur.fetchone()[0]:
        return

    def _insert_template(payload: dict, elements: list[dict]) -> None:
        conn.execute(
            """
            INSERT INTO LabelTemplates (
                code, title, kind, page_w_mm, page_h_mm, orientation, cols, rows,
                label_w_mm, label_h_mm, gap_x_mm, gap_y_mm, margin_left_mm, margin_top_mm,
                margin_right_mm, margin_bottom_mm, offset_x_mm, offset_y_mm, scale_x, scale_y,
                is_active, is_default
            ) VALUES (
                :code, :title, :kind, :page_w_mm, :page_h_mm, :orientation, :cols, :rows,
                :label_w_mm, :label_h_mm, :gap_x_mm, :gap_y_mm, :margin_left_mm, :margin_top_mm,
                :margin_right_mm, :margin_bottom_mm, :offset_x_mm, :offset_y_mm, :scale_x, :scale_y,
                :is_active, :is_default
            )
            """,
            payload,
        )
        tpl_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for order, element in enumerate(elements):
            element = {**element, "template_id": tpl_id, "sort_order": order}
            element["options_json"] = json.dumps(element.get("options", {}))
            conn.execute(
                """
                INSERT INTO LabelTemplateElements (
                    template_id, element_type, field_key, x_mm, y_mm, w_mm, h_mm,
                    rotation_deg, align, font_name, font_size, max_chars, wrap, options_json,
                    sort_order, is_active
                ) VALUES (
                    :template_id, :element_type, :field_key, :x_mm, :y_mm, :w_mm, :h_mm,
                    :rotation_deg, :align, :font_name, :font_size, :max_chars, :wrap, :options_json,
                    :sort_order, :is_active
                )
                """,
                element,
            )

    a4_template = {
        "code": "A4_3x8_70x35",
        "title": "A4 70x35 3x8",
        "kind": "sheet",
        "page_w_mm": 210,
        "page_h_mm": 297,
        "orientation": "portrait",
        "cols": 3,
        "rows": 8,
        "label_w_mm": 68,
        "label_h_mm": 35,
        "gap_x_mm": 2,
        "gap_y_mm": 2,
        "margin_left_mm": 0,
        "margin_top_mm": 0,
        "margin_right_mm": 0,
        "margin_bottom_mm": 0,
        "offset_x_mm": 0,
        "offset_y_mm": 0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "is_active": 1,
        "is_default": 1,
    }
    a4_elements = [
        {
            "element_type": "barcode",
            "field_key": "code",
            "x_mm": 2,
            "y_mm": 14,
            "w_mm": 64,
            "h_mm": 17,
            "rotation_deg": 0,
            "align": "center",
            "font_name": "Helvetica",
            "font_size": 9,
            "max_chars": None,
            "wrap": 0,
            "options": {"bar_height_mm": 12, "human_readable": False},
            "is_active": 1,
        },
        {
            "element_type": "text",
            "field_key": "code",
            "x_mm": 2,
            "y_mm": 12,
            "w_mm": 64,
            "h_mm": 4,
            "align": "center",
            "font_name": "IL_SANS",
            "font_size": 8,
            "max_chars": 32,
            "wrap": 0,
            "options": {"text_template": "{code}"},
            "rotation_deg": 0,
            "is_active": 1,
        },
        {
            "element_type": "text",
            "field_key": "name",
            "x_mm": 2,
            "y_mm": 2,
            "w_mm": 64,
            "h_mm": 8,
            "align": "left",
            "font_name": "IL_SANS",
            "font_size": 9,
            "max_chars": 40,
            "wrap": 0,
            "options": {"text_template": "{name}"},
            "rotation_deg": 0,
            "is_active": 1,
        },
    ]

    thermal_template = {
        "code": "THERMAL_58x40",
        "title": "Термал 58x40",
        "kind": "thermal",
        "page_w_mm": 58,
        "page_h_mm": 40,
        "orientation": "portrait",
        "cols": 1,
        "rows": 1,
        "label_w_mm": 58,
        "label_h_mm": 40,
        "gap_x_mm": 0,
        "gap_y_mm": 0,
        "margin_left_mm": 0,
        "margin_top_mm": 0,
        "margin_right_mm": 0,
        "margin_bottom_mm": 0,
        "offset_x_mm": 0,
        "offset_y_mm": 0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "is_active": 1,
        "is_default": 0,
    }
    thermal_elements = [
        {
            "element_type": "barcode",
            "field_key": "code",
            "x_mm": 2,
            "y_mm": 20,
            "w_mm": 54,
            "h_mm": 16,
            "rotation_deg": 0,
            "align": "center",
            "font_name": "Helvetica",
            "font_size": 9,
            "max_chars": None,
            "wrap": 0,
            "options": {"bar_height_mm": 12, "human_readable": False},
            "is_active": 1,
        },
        {
            "element_type": "text",
            "field_key": "code",
            "x_mm": 2,
            "y_mm": 16,
            "w_mm": 54,
            "h_mm": 4,
            "align": "center",
            "font_name": "IL_SANS",
            "font_size": 8,
            "max_chars": 32,
            "wrap": 0,
            "options": {"text_template": "{code}"},
            "rotation_deg": 0,
            "is_active": 1,
        },
        {
            "element_type": "text",
            "field_key": "name",
            "x_mm": 2,
            "y_mm": 4,
            "w_mm": 54,
            "h_mm": 10,
            "align": "left",
            "font_name": "IL_SANS",
            "font_size": 9,
            "max_chars": 40,
            "wrap": 0,
            "options": {"text_template": "{name}"},
            "rotation_deg": 0,
            "is_active": 1,
        },
    ]

    _insert_template(a4_template, a4_elements)
    _insert_template(thermal_template, thermal_elements)
    _ensure_column(conn, "CashTransactions", "related_doc_type", "TEXT")
    _ensure_column(conn, "CashTransactions", "related_doc_id", "INTEGER")
    _ensure_column(conn, "CashTransactions", "channel", "TEXT")
    _ensure_column(conn, "CashTransactions", "comment", "TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ProductCategoryLinks (
            product_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            PRIMARY KEY (product_id, category_id),
            FOREIGN KEY (product_id) REFERENCES Products(id) ON DELETE CASCADE,
            FOREIGN KEY (category_id) REFERENCES Categories(id) ON DELETE CASCADE
        )
        """
    )

    # Preserve legacy additional category links stored in the deprecated
    # AdditionalProductCategories table.
    conn.execute(
        """
        INSERT OR IGNORE INTO ProductCategoryLinks (product_id, category_id)
        SELECT product_id, category_id FROM AdditionalProductCategories
        """
    )

    _migrate_stock_balances(conn)

    _ensure_currency_tables(conn)


def _ensure_currency_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO Currencies (code, name, decimals, is_active) VALUES (?,?,?,1)",
        (
            utils.get_base_currency_code(),
            utils.get_base_currency_name(),
            utils.get_base_currency_decimals(),
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO Currencies (code, name, decimals, is_active) VALUES ('UAH', 'Українська гривня', 2, 1)"
    )
    has_rate = conn.execute(
        "SELECT 1 FROM CurrencyRates WHERE currency_code=? LIMIT 1",
        (utils.get_base_currency_code(),),
    ).fetchone()
    if not has_rate:
        conn.execute(
            "INSERT INTO CurrencyRates (currency_code, rate_date, rate) VALUES (?, date('now'), 1)",
            (utils.get_base_currency_code(),),
        )


def _migrate_stock_balances(conn: sqlite3.Connection) -> None:
    """Rebuild StockBalances table if it misses expected columns or PK."""

    cur = conn.execute("PRAGMA table_info(StockBalances)")
    columns = {row[1]: row[5] for row in cur.fetchall()}  # name -> pk position
    expected_columns = {"product_id", "warehouse_id", "quantity", "average_cost", "updated_at"}
    has_all_columns = expected_columns.issubset(columns)
    has_composite_pk = columns.get("product_id") and columns.get("warehouse_id")

    if has_all_columns and has_composite_pk:
        return

    logging.info("Rebuilding StockBalances schema to include warehouse-level balances")
    conn.execute("ALTER TABLE StockBalances RENAME TO StockBalances_old")
    conn.execute(
        """
        CREATE TABLE StockBalances (
            product_id INTEGER NOT NULL,
            warehouse_id INTEGER NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            average_cost REAL NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (product_id, warehouse_id),
            FOREIGN KEY (product_id) REFERENCES Products(id),
            FOREIGN KEY (warehouse_id) REFERENCES Warehouses(id)
        )
        """
    )

    default_wh = conn.execute("SELECT id FROM Warehouses ORDER BY id LIMIT 1").fetchone()
    if not default_wh:
        default_wh_id = conn.execute(
            "INSERT INTO Warehouses (name, description, is_active) VALUES ('Main warehouse','',1)"
        ).lastrowid
    else:
        default_wh_id = int(default_wh[0])

    old_columns = {row[1] for row in conn.execute("PRAGMA table_info(StockBalances_old)").fetchall()}
    if "warehouse_id" in old_columns:
        conn.execute(
            """
            INSERT INTO StockBalances (product_id, warehouse_id, quantity, average_cost, updated_at)
            SELECT product_id, warehouse_id, quantity, average_cost, updated_at
            FROM StockBalances_old
            """
        )
    else:
        conn.execute(
            """
            INSERT INTO StockBalances (product_id, warehouse_id, quantity, average_cost, updated_at)
            SELECT product_id, ?, quantity, average_cost, updated_at
            FROM StockBalances_old
            """,
            (default_wh_id,),
        )

    conn.execute("DROP TABLE StockBalances_old")


# Brand CRUD

def list_brands() -> List[sqlite3.Row]:
    with get_connection() as conn:
        return list(conn.execute("SELECT id, name FROM Brands ORDER BY name"))


def add_brand(name: str) -> int:
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO Brands (name) VALUES (?)", (name.strip(),))
        conn.commit()
        return cur.lastrowid


def update_brand(brand_id: int, name: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE Brands SET name=? WHERE id=?", (name.strip(), brand_id))
        conn.commit()


def delete_brand(brand_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM Brands WHERE id=?", (brand_id,))
        conn.commit()


# Category CRUD with hierarchy and flags

def list_categories(include_hidden: bool = True) -> List[sqlite3.Row]:
    query = "SELECT id, name, parent_id, sort_order, is_service, is_hidden, color, icon, typical_attributes FROM Categories"
    if not include_hidden:
        query += " WHERE IFNULL(is_hidden,0)=0"
    query += " ORDER BY parent_id NULLS FIRST, sort_order, name"
    with get_connection() as conn:
        return list(conn.execute(query))


def list_categories_tree(include_hidden: bool = True) -> list[dict]:
    """
    Повертає категорії у вигляді плоского списку в pre-order (дерево),
    кожен елемент: dict з полями Categories + depth + label.
    label = "    " * depth + name
    """
    rows = list_categories(include_hidden=include_hidden)
    items = [dict(r) for r in rows]

    by_id = {c["id"]: c for c in items}
    children: dict[object, list[dict]] = {}
    for c in items:
        pid = c.get("parent_id", None)
        if pid == 0:
            pid = None
            c["parent_id"] = None
        children.setdefault(pid, []).append(c)

    def sort_key(x: dict):
        return (int(x.get("sort_order") or 0), (x.get("name") or "").lower())

    for pid, arr in children.items():
        arr.sort(key=sort_key)

    roots = list(children.get(None, []))
    for c in items:
        pid = c.get("parent_id")
        if pid is not None and pid not in by_id:
            roots.append(c)

    seen_root = set()
    uniq_roots = []
    for r in roots:
        if r["id"] in seen_root:
            continue
        seen_root.add(r["id"])
        uniq_roots.append(r)
    uniq_roots.sort(key=sort_key)

    result: list[dict] = []
    visited: set[int] = set()

    def walk(node: dict, depth: int) -> None:
        nid = node["id"]
        if nid in visited:
            return
        visited.add(nid)
        out = dict(node)
        out["depth"] = depth
        out["label"] = ("    " * depth) + (out.get("name") or "")
        result.append(out)
        for ch in children.get(nid, []):
            walk(ch, depth + 1)

    for r in uniq_roots:
        walk(r, 0)

    for c in items:
        if c["id"] not in visited:
            out = dict(c)
            out["depth"] = 0
            out["label"] = out.get("name") or ""
            result.append(out)

    return result


def _next_sort_order(conn: sqlite3.Connection, parent_id: Optional[int]) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order),0) FROM Categories WHERE parent_id IS ?",
        (parent_id,),
    ).fetchone()
    return int(row[0]) + 1


def add_category(
    name: str,
    parent_id: Optional[int] = None,
    color: str | None = None,
    icon: str | None = None,
    typical_attributes: str | None = None,
    is_service: bool = False,
    is_hidden: bool = False,
) -> int:
    with get_connection() as conn:
        sort_order = _next_sort_order(conn, parent_id)
        cur = conn.execute(
            """
            INSERT INTO Categories (name, parent_id, sort_order, color, icon, typical_attributes, is_service, is_hidden)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name.strip(),
                parent_id,
                sort_order,
                color.strip() if color else None,
                icon.strip() if icon else None,
                typical_attributes.strip() if typical_attributes else None,
                1 if is_service else 0,
                1 if is_hidden else 0,
            ),
        )
        conn.commit()
        return cur.lastrowid


def update_category(
    category_id: int,
    name: str,
    parent_id: Optional[int] = None,
    color: str | None = None,
    icon: str | None = None,
    typical_attributes: str | None = None,
    is_service: bool = False,
    is_hidden: bool = False,
    sort_order: Optional[int] = None,
) -> None:
    with get_connection() as conn:
        if sort_order is None:
            sort_row = conn.execute("SELECT sort_order FROM Categories WHERE id=?", (category_id,)).fetchone()
            sort_order = int(sort_row[0]) if sort_row else 0
        conn.execute(
            """
            UPDATE Categories
            SET name=?, parent_id=?, sort_order=?, color=?, icon=?, typical_attributes=?, is_service=?, is_hidden=?
            WHERE id=?
            """,
            (
                name.strip(),
                parent_id,
                sort_order,
                color.strip() if color else None,
                icon.strip() if icon else None,
                typical_attributes.strip() if typical_attributes else None,
                1 if is_service else 0,
                1 if is_hidden else 0,
                category_id,
            ),
        )
        conn.commit()


def bump_category_order(category_id: int, delta: int) -> None:
    with get_connection() as conn:
        row = conn.execute("SELECT parent_id, sort_order FROM Categories WHERE id=?", (category_id,)).fetchone()
        if not row:
            return
        parent_id, current_order = row["parent_id"], int(row["sort_order"] or 0)
        sibling = conn.execute(
            """
            SELECT id, sort_order FROM Categories
            WHERE parent_id IS ? AND sort_order * ? < ?
            ORDER BY sort_order * ? DESC, name
            LIMIT 1
            """,
            (parent_id, delta, current_order * delta, delta),
        ).fetchone()
        if not sibling:
            return
        conn.execute(
            "UPDATE Categories SET sort_order=? WHERE id=?",
            (int(sibling["sort_order"] or 0), category_id),
        )
        conn.execute(
            "UPDATE Categories SET sort_order=? WHERE id=?",
            (current_order, int(sibling["id"])),
        )
        conn.commit()


def move_category(category_id: int, new_parent_id: Optional[int]) -> None:
    with get_connection() as conn:
        new_order = _next_sort_order(conn, new_parent_id)
        conn.execute(
            "UPDATE Categories SET parent_id=?, sort_order=? WHERE id=?",
            (new_parent_id, new_order, category_id),
        )
        conn.commit()


def get_category_descendants(category_id: int) -> List[int]:
    with get_connection() as conn:
        rows = list(conn.execute("SELECT id, parent_id FROM Categories"))
    children_map = {}
    for row in rows:
        children_map.setdefault(row["parent_id"], []).append(row["id"])

    result = []

    def _walk(node_id: int) -> None:
        for child in children_map.get(node_id, []):
            result.append(child)
            _walk(child)

    _walk(category_id)
    return result


def delete_category(category_id: int, target_category_id: Optional[int] = None) -> None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT is_service FROM Categories WHERE id=?",
            (category_id,),
        ).fetchone()
        if not row:
            return
        if row["is_service"]:
            raise ValueError("Службову категорію не можна видалити")

        has_children = conn.execute("SELECT 1 FROM Categories WHERE parent_id=? LIMIT 1", (category_id,)).fetchone()
        has_products = conn.execute("SELECT 1 FROM Products WHERE category_id=? LIMIT 1", (category_id,)).fetchone()
        has_links = conn.execute(
            "SELECT 1 FROM ProductCategoryLinks WHERE category_id=? LIMIT 1",
            (category_id,),
        ).fetchone()

        if (has_children or has_products or has_links) and not target_category_id:
            raise ValueError("Категорія містить дані — оберіть ціль для злиття")

        if target_category_id:
            if target_category_id == category_id or target_category_id in get_category_descendants(category_id):
                raise ValueError("Ціль не може бути підкатегорією вихідної")
            # Move children
            conn.execute(
                "UPDATE Categories SET parent_id=? WHERE parent_id=?",
                (target_category_id, category_id),
            )
            # Move main categories
            conn.execute(
                "UPDATE Products SET category_id=? WHERE category_id=?",
                (target_category_id, category_id),
            )
            # Move additional links
            conn.execute(
                "INSERT OR IGNORE INTO ProductCategoryLinks (product_id, category_id)\n"
                "SELECT product_id, ? FROM ProductCategoryLinks WHERE category_id=?",
                (target_category_id, category_id),
            )
            conn.execute(
                "DELETE FROM ProductCategoryLinks WHERE category_id=?",
                (category_id,),
            )

        conn.execute("DELETE FROM Categories WHERE id=?", (category_id,))
        conn.commit()


def category_product_counts(include_hidden: bool = True) -> Dict[int, int]:
    stats = category_inventory_stats(include_hidden)
    return {cid: values["products"] for cid, values in stats.items()}


def category_inventory_data(include_hidden: bool = True) -> Tuple[Dict[int, Set[int]], Dict[int, float]]:
    """Return category -> products mapping and product -> total quantity.

    Both main and additional category links are included. Hidden categories can be
    excluded with ``include_hidden=False``.
    """

    where_clause = ""
    if not include_hidden:
        where_clause = "WHERE IFNULL(c.is_hidden,0)=0"

    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT p.id AS product_id, p.category_id AS category_id FROM Products p JOIN Categories c ON c.id = p.category_id {where_clause}"
        ).fetchall()
        linked_rows = conn.execute(
            f"SELECT l.product_id, l.category_id FROM ProductCategoryLinks l JOIN Categories c ON c.id = l.category_id {where_clause}"
        ).fetchall()
        quantities = conn.execute(
            "SELECT product_id, IFNULL(SUM(quantity), 0) AS qty FROM StockBalances GROUP BY product_id"
        ).fetchall()

    category_products: Dict[int, Set[int]] = {}
    for row in rows + linked_rows:
        category_products.setdefault(int(row["category_id"]), set()).add(int(row["product_id"]))

    product_quantities = {int(row["product_id"]): float(row["qty"]) for row in quantities}
    return category_products, product_quantities


def category_inventory_stats(include_hidden: bool = True) -> Dict[int, Dict[str, float | int]]:
    """Return per-category stats: product count and total stock quantity."""

    category_products, product_quantities = category_inventory_data(include_hidden)
    stats: Dict[int, Dict[str, float | int]] = {}
    for cid, products in category_products.items():
        stats[cid] = {
            "products": len(products),
            "quantity": sum(product_quantities.get(pid, 0.0) for pid in products),
        }
    return stats


# Product CRUD

def list_products(
    search: Optional[str] = None,
    category_id: Optional[int] = None,
    include_subcategories: bool = False,
) -> List[sqlite3.Row]:
    base_query = (
        "SELECT p.id, p.sku, p.supplier_sku, p.name, p.unit, p.is_active, b.name AS brand, c.name AS category, "
        "p.brand_id, p.category_id, IFNULL(pb.barcodes, '') AS barcodes, "
        "REPLACE(GROUP_CONCAT(DISTINCT c2.name), ',', ', ') AS extra_categories "
        "FROM Products p "
        "JOIN Brands b ON p.brand_id = b.id "
        "JOIN Categories c ON p.category_id = c.id "
        "LEFT JOIN ProductCategoryLinks pcl ON pcl.product_id = p.id "
        "LEFT JOIN Categories c2 ON c2.id = pcl.category_id "
        "LEFT JOIN (SELECT product_id, GROUP_CONCAT(code, ' ') AS barcodes FROM ProductBarcodes GROUP BY product_id) pb ON pb.product_id = p.id "
    )
    where_clauses = []
    params: List[int | str] = []

    if category_id:
        target_ids = [category_id]
        if include_subcategories:
            target_ids.extend(get_category_descendants(category_id))
        placeholders = ",".join("?" * len(target_ids))
        where_clauses.append(
            f"(p.category_id IN ({placeholders}) OR p.id IN (SELECT product_id FROM ProductCategoryLinks WHERE category_id IN ({placeholders})))"
        )
        params.extend(target_ids)
        params.extend(target_ids)

    where = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    query = (
        base_query
        + where
        + " GROUP BY p.id, p.sku, p.supplier_sku, p.name, p.unit, p.is_active, b.name, c.name, p.brand_id, p.category_id"
        + " ORDER BY p.name"
    )

    with get_connection() as conn:
        rows = list(conn.execute(query, tuple(params)))
    if not search:
        return rows

    needle = norm_text(search)
    filtered: list[sqlite3.Row] = []
    for row in rows:
        haystack_parts = [
            row["sku"],
            row["name"],
            row["supplier_sku"] or "",
            row["brand"],
            row["category"],
            row["extra_categories"] or "",
            row["barcodes"] or "",
        ]
        haystack = norm_text(" ".join(haystack_parts))
        if needle in haystack:
            filtered.append(row)
    return filtered


def list_skus_by_prefix(prefix: str) -> List[sqlite3.Row]:
    with get_connection() as conn:
        return list(conn.execute("SELECT sku FROM Products WHERE sku LIKE ?", (f"{prefix}%",)))


def add_product(
    sku: str,
    name: str,
    brand_id: int,
    category_id: int,
    unit: str = "pcs",
    is_active: bool = True,
    supplier_sku: str | None = None,
) -> int:
    normalized_sku = normalize_sku(sku)
    normalized_supplier = normalize_supplier_sku(supplier_sku)
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO Products (sku, supplier_sku, name, brand_id, category_id, unit, is_active) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                normalized_sku,
                normalized_supplier,
                name.strip(),
                brand_id,
                category_id,
                unit.strip() or "pcs",
                1 if is_active else 0,
            ),
        )
        conn.commit()
        return cur.lastrowid


def find_product_by_sku_or_name(
    sku: Optional[str], name: Optional[str], supplier_sku: Optional[str] = None
) -> Optional[sqlite3.Row]:
    """Find product by SKU, supplier SKU, or name (case-insensitive)."""

    sku = (sku or "").strip()
    name = (name or "").strip()
    supplier_sku = (supplier_sku or "").strip()
    clauses: List[str] = []
    params: List[str] = []
    if sku:
        try:
            normalized_sku = normalize_sku(sku)
        except ValueError:
            normalized_sku = ""
        if normalized_sku:
            clauses.append("lower(sku)=?")
            params.append(normalized_sku.lower())
    if supplier_sku:
        normalized_supplier = normalize_supplier_sku(supplier_sku)
        if normalized_supplier:
            clauses.append("lower(supplier_sku)=?")
            params.append(normalized_supplier.lower())
    if name:
        clauses.append("lower(name)=?")
        params.append(name.lower())
    if not clauses:
        return None
    where = " OR ".join(clauses)
    with get_connection() as conn:
        return conn.execute(
            f"SELECT id, sku, supplier_sku, name, brand_id, category_id, unit, is_active FROM Products WHERE {where} LIMIT 1",
            tuple(params),
        ).fetchone()


def list_product_supplier_codes(product_id: int) -> list[sqlite3.Row]:
    """
    Returns rows with: id, supplier_id, supplier_name, supplier_sku, is_primary
    """

    query = (
        """
        SELECT psc.id, psc.supplier_id, c.name AS supplier_name, psc.supplier_sku, psc.is_primary
        FROM ProductSupplierCodes psc
        JOIN Counterparties c ON psc.supplier_id = c.id
        WHERE psc.product_id = ?
        ORDER BY c.name, psc.supplier_sku
        """
    )
    with get_connection() as conn:
        return list(conn.execute(query, (product_id,)))


def replace_product_supplier_codes(product_id: int, codes: list[dict]) -> None:
    """
    Replace all supplier codes for a product in one transaction:
    - delete existing for product_id
    - insert all provided codes
    - ensure at most one primary per supplier_id (and optionally at most one global primary)
    Raises sqlite3.IntegrityError on UNIQUE conflicts (supplier_id, supplier_sku).
    """

    unique_keys = set()
    primary_seen = set()
    sanitized: list[tuple[int, int, str, int]] = []
    for code in codes or []:
        try:
            supplier_id = int(code.get("supplier_id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        supplier_sku = normalize_supplier_sku(code.get("supplier_sku"))
        if not supplier_id or not supplier_sku:
            continue
        key = (supplier_id, supplier_sku.lower())
        if key in unique_keys:
            continue
        unique_keys.add(key)
        is_primary = 1 if code.get("is_primary") else 0
        if is_primary:
            if supplier_id in primary_seen:
                is_primary = 0
            else:
                primary_seen.add(supplier_id)
        sanitized.append((product_id, supplier_id, supplier_sku, is_primary))

    with get_connection() as conn:
        with safe_transaction(conn):
            conn.execute("DELETE FROM ProductSupplierCodes WHERE product_id=?", (product_id,))
            if sanitized:
                conn.executemany(
                    "INSERT INTO ProductSupplierCodes (product_id, supplier_id, supplier_sku, is_primary) VALUES (?,?,?,?)",
                    sanitized,
                )


def get_product_id_by_channel_sku(channel_id: int, external_sku: str) -> Optional[int]:
    external_sku = normalize_external_sku(external_sku)
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT product_id
            FROM ProductChannelCodes
            WHERE channel_id=? AND lower(trim(external_sku))=lower(trim(?)) AND is_active=1
            LIMIT 1
            """,
            (channel_id, external_sku),
        ).fetchone()
        return int(row["product_id"]) if row else None


def list_channel_codes_for_product(product_id: int) -> list[sqlite3.Row]:
    query = """
        SELECT pcc.id,
               pcc.channel_id,
               sc.name AS channel_name,
               pcc.external_sku,
               pcc.external_name,
               pcc.is_primary,
               pcc.is_active,
               pcc.last_seen_at,
               pcc.note
        FROM ProductChannelCodes pcc
        JOIN SalesChannels sc ON sc.id = pcc.channel_id
        WHERE pcc.product_id=?
        ORDER BY sc.name, pcc.external_sku
    """
    with get_connection() as conn:
        return list(conn.execute(query, (product_id,)))


def list_all_channel_codes() -> list[sqlite3.Row]:
    query = """
        SELECT pcc.id,
               sc.name AS channel_name,
               pcc.external_sku,
               p.sku AS internal_sku,
               pcc.is_active,
               pcc.note,
               pcc.last_seen_at
        FROM ProductChannelCodes pcc
        JOIN SalesChannels sc ON sc.id = pcc.channel_id
        JOIN Products p ON p.id = pcc.product_id
        ORDER BY sc.name, pcc.external_sku
    """
    with get_connection() as conn:
        return list(conn.execute(query))


def upsert_channel_code(
    channel_id: int,
    product_id: int,
    external_sku: str,
    external_name: str | None = None,
    is_primary: int = 1,
    is_active: int = 1,
    note: str | None = None,
) -> int:
    external_sku_clean = normalize_external_sku(external_sku)
    now = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, product_id FROM ProductChannelCodes
            WHERE channel_id=? AND lower(trim(external_sku))=lower(trim(?))
            LIMIT 1
            """,
            (channel_id, external_sku_clean),
        ).fetchone()
        if row:
            existing_id = int(row["id"])
            try:
                conn.execute(
                    """
                    UPDATE ProductChannelCodes
                    SET product_id=?, is_primary=?, is_active=?, external_name=?, last_seen_at=?, note=?
                    WHERE id=?
                    """,
                    (
                        product_id,
                        1 if is_primary else 0,
                        1 if is_active else 0,
                        (external_name or "").strip() or None,
                        now,
                        (note or "").strip() or None,
                        existing_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"SKU '{external_sku_clean}' вже використовується для іншого товару у цьому каналі.") from exc
            conn.commit()
            return existing_id
        try:
            cur = conn.execute(
                """
                INSERT INTO ProductChannelCodes (
                    channel_id, product_id, external_sku, external_name, is_primary, is_active, last_seen_at, note
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    channel_id,
                    product_id,
                    external_sku_clean,
                    (external_name or "").strip() or None,
                    1 if is_primary else 0,
                    1 if is_active else 0,
                    now,
                    (note or "").strip() or None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"SKU '{external_sku_clean}' вже прив'язаний до іншого товару у цьому каналі.") from exc
        conn.commit()
        return int(cur.lastrowid)


def delete_channel_code(id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM ProductChannelCodes WHERE id=?", (id,))
        conn.commit()


def touch_channel_code_seen(channel_id: int, external_sku: str, external_name: str | None = None) -> None:
    external_sku_clean = normalize_external_sku(external_sku)
    now = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE ProductChannelCodes
            SET last_seen_at=?, external_name=COALESCE(?, external_name)
            WHERE channel_id=? AND lower(trim(external_sku))=lower(trim(?))
            """,
            (now, (external_name or "").strip() or None, channel_id, external_sku_clean),
        )
        conn.commit()


def bulk_upsert_channel_codes(rows: list[dict]) -> dict:
    inserted = updated = skipped = 0
    errors: list[str] = []
    for idx, row in enumerate(rows, start=1):
        try:
            channel_id = int(row.get("channel_id"))  # type: ignore[arg-type]
            product_id = int(row.get("product_id"))  # type: ignore[arg-type]
            external_sku = normalize_external_sku(row.get("external_sku") or "")
        except Exception:
            skipped += 1
            errors.append(f"Рядок {idx}: некоректні channel_id/product_id/external_sku")
            continue
        external_name = row.get("external_name")
        is_primary = 1 if row.get("is_primary", 1) else 0
        is_active = 1 if row.get("is_active", 1) else 0
        note = row.get("note")
        try:
            existing = get_product_id_by_channel_sku(channel_id, external_sku)
            upsert_channel_code(
                channel_id,
                product_id,
                external_sku,
                external_name=external_name,
                is_primary=is_primary,
                is_active=is_active,
                note=note,
            )
            if existing and existing == product_id:
                updated += 1
            elif existing:
                updated += 1
            else:
                inserted += 1
        except ValueError as exc:
            errors.append(f"Рядок {idx}: {exc}")
            skipped += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors}


def find_products_by_internal_sku(internal_sku: str) -> list[dict]:
    try:
        normalized = normalize_sku(internal_sku)
    except ValueError:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, sku, name FROM Products WHERE lower(trim(sku))=lower(trim(?))",
            (normalized,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_sales_channels() -> List[sqlite3.Row]:
    return list_channels(active_only=False)


def list_product_barcodes(product_id: int) -> list[sqlite3.Row]:
    query = "SELECT id, code, note FROM ProductBarcodes WHERE product_id=? ORDER BY id"
    with get_connection() as conn:
        return list(conn.execute(query, (product_id,)))


def replace_product_barcodes(product_id: int, codes: list[dict]) -> None:
    unique_codes = set()
    sanitized: list[tuple[int, str, str | None]] = []
    for code in codes or []:
        try:
            normalized = normalize_barcode(code.get("code") or "")
        except ValueError:
            continue
        if normalized in unique_codes:
            continue
        unique_codes.add(normalized)
        note = (code.get("note") or "").strip()
        sanitized.append((product_id, normalized, note or None))

    with get_connection() as conn:
        with safe_transaction(conn):
            conn.execute("DELETE FROM ProductBarcodes WHERE product_id=?", (product_id,))
            if sanitized:
                conn.executemany(
                    "INSERT INTO ProductBarcodes (product_id, code, note) VALUES (?,?,?)",
                    sanitized,
                )


def list_product_images(product_id: int) -> list[sqlite3.Row]:
    query = """
        SELECT id, product_id, rel_path, original_name, sort_order, is_primary, created_at
        FROM ProductImages
        WHERE product_id=?
        ORDER BY sort_order, id
    """
    with get_connection() as conn:
        return list(conn.execute(query, (product_id,)))


def get_product_image(image_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT id, product_id, rel_path, original_name, sort_order, is_primary, created_at
            FROM ProductImages WHERE id=?
            """,
            (image_id,),
        ).fetchone()


def add_product_image(
    *,
    product_id: int,
    rel_path: str,
    original_name: str | None,
    sort_order: int,
    is_primary: bool = False,
) -> int:
    with get_connection() as conn:
        with safe_transaction(conn):
            if is_primary:
                conn.execute("UPDATE ProductImages SET is_primary=0 WHERE product_id=?", (product_id,))
            cur = conn.execute(
                """
                INSERT INTO ProductImages (product_id, rel_path, original_name, sort_order, is_primary)
                VALUES (?,?,?,?,?)
                """,
                (product_id, rel_path, original_name, sort_order, 1 if is_primary else 0),
            )
        return int(cur.lastrowid)


def delete_product_image(image_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM ProductImages WHERE id=?", (image_id,))
        conn.commit()


def set_primary_product_image(image_id: int) -> None:
    with get_connection() as conn:
        with safe_transaction(conn):
            row = conn.execute("SELECT product_id FROM ProductImages WHERE id=?", (image_id,)).fetchone()
            if not row:
                return
            product_id = row["product_id"]
            conn.execute("UPDATE ProductImages SET is_primary=0 WHERE product_id=?", (product_id,))
            conn.execute("UPDATE ProductImages SET is_primary=1 WHERE id=? AND product_id=?", (image_id, product_id))


def find_product_by_scan_code(code: str, barcode_prefix: str = "") -> Optional[sqlite3.Row]:
    clean_code = _strip_weird(code).strip()
    if not clean_code:
        return None
    prefix = (barcode_prefix or "").strip()
    try:
        normalized_code = normalize_barcode(clean_code)
    except ValueError:
        return None
    lower_code = normalized_code.lower()

    product_query = (
        "SELECT id, sku, supplier_sku, name, brand_id, category_id, unit, is_active FROM Products WHERE lower(sku)=? LIMIT 1"
    )

    with get_connection() as conn:
        row = conn.execute(product_query, (lower_code,)).fetchone()
        if row:
            return row

        if prefix and lower_code.startswith(prefix.lower()):
            stripped = clean_code[len(prefix) :]
            if stripped:
                try:
                    normalized_stripped = normalize_sku(stripped)
                except ValueError:
                    normalized_stripped = ""
                if normalized_stripped:
                    row = conn.execute(product_query, (normalized_stripped.lower(),)).fetchone()
                    if row:
                        return row

        alias_row = conn.execute(
            """
            SELECT p.id, p.sku, p.supplier_sku, p.name, p.brand_id, p.category_id, p.unit, p.is_active
            FROM ProductBarcodes pb
            JOIN Products p ON p.id = pb.product_id
            WHERE lower(pb.code)=?
            LIMIT 1
            """,
            (lower_code,),
        ).fetchone()
        return alias_row


def get_product_by_supplier_code(supplier_id: int, supplier_sku: str) -> sqlite3.Row | None:
    """
    Finds product by (supplier_id, supplier_sku) case-insensitive.
    Returns product row (at least id, sku, name, brand_id, category_id, unit, is_active).
    """

    supplier_sku = normalize_supplier_sku(supplier_sku)
    if not supplier_sku:
        return None
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT p.id, p.sku, p.supplier_sku, p.name, p.brand_id, p.category_id, p.unit, p.is_active
            FROM ProductSupplierCodes psc
            JOIN Products p ON p.id = psc.product_id
            WHERE psc.supplier_id = ? AND lower(psc.supplier_sku) = lower(?)
            LIMIT 1
            """,
            (supplier_id, supplier_sku),
        ).fetchone()


def update_product(
    product_id: int,
    sku: str,
    name: str,
    brand_id: int,
    category_id: int,
    unit: str = "pcs",
    is_active: bool = True,
    supplier_sku: str | None = None,
) -> None:
    normalized_sku = normalize_sku(sku)
    normalized_supplier = normalize_supplier_sku(supplier_sku)
    with get_connection() as conn:
        conn.execute(
            "UPDATE Products SET sku=?, supplier_sku=?, name=?, brand_id=?, category_id=?, unit=?, is_active=? WHERE id=?",
            (
                normalized_sku,
                normalized_supplier,
                name.strip(),
                brand_id,
                category_id,
                unit.strip() or "pcs",
                1 if is_active else 0,
                product_id,
            ),
        )
        conn.commit()


def set_product_categories(product_id: int, category_id: int, additional_category_ids: Sequence[int] | None) -> None:
    additional_category_ids = list(dict.fromkeys(additional_category_ids or []))
    with get_connection() as conn:
        conn.execute("UPDATE Products SET category_id=? WHERE id=?", (category_id, product_id))
        conn.execute("DELETE FROM ProductCategoryLinks WHERE product_id=?", (product_id,))
        for cid in additional_category_ids:
            if cid == category_id:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO ProductCategoryLinks (product_id, category_id) VALUES (?,?)",
                (product_id, cid),
        )
        conn.commit()


def bulk_update_products_is_active(conn: sqlite3.Connection, product_ids: list[int], is_active: int) -> int:
    if not product_ids:
        return 0
    placeholders = ",".join("?" * len(product_ids))
    with safe_transaction(conn):
        cur = conn.execute(
            f"UPDATE Products SET is_active=? WHERE id IN ({placeholders})", (is_active, *product_ids)
        )
        return cur.rowcount


def bulk_update_products_brand(conn: sqlite3.Connection, product_ids: list[int], brand_id: int) -> int:
    if not product_ids:
        return 0
    placeholders = ",".join("?" * len(product_ids))
    with safe_transaction(conn):
        cur = conn.execute(
            f"UPDATE Products SET brand_id=? WHERE id IN ({placeholders})", (brand_id, *product_ids)
        )
        return cur.rowcount


def bulk_update_products_category(conn: sqlite3.Connection, product_ids: list[int], category_id: int) -> int:
    if not product_ids:
        return 0
    placeholders = ",".join("?" * len(product_ids))
    with safe_transaction(conn):
        cur = conn.execute(
            f"UPDATE Products SET category_id=? WHERE id IN ({placeholders})", (category_id, *product_ids)
        )
        return cur.rowcount


def bulk_update_products_unit(conn: sqlite3.Connection, product_ids: list[int], unit: str) -> int:
    if not product_ids:
        return 0
    placeholders = ",".join("?" * len(product_ids))
    with safe_transaction(conn):
        cur = conn.execute(
            f"UPDATE Products SET unit=? WHERE id IN ({placeholders})", (unit.strip() or "pcs", *product_ids)
        )
        return cur.rowcount


def bulk_add_product_category_links(
    conn: sqlite3.Connection, product_ids: list[int], category_ids: list[int]
) -> int:
    if not product_ids or not category_ids:
        return 0
    with safe_transaction(conn):
        inserted = 0
        for pid in product_ids:
            for cid in category_ids:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO ProductCategoryLinks (product_id, category_id) VALUES (?,?)",
                    (pid, cid),
                )
                inserted += cur.rowcount
        return inserted


def bulk_remove_product_category_links(
    conn: sqlite3.Connection, product_ids: list[int], category_ids: list[int]
) -> int:
    if not product_ids or not category_ids:
        return 0
    with safe_transaction(conn):
        deleted = 0
        for pid in product_ids:
            placeholders = ",".join("?" * len(category_ids))
            cur = conn.execute(
                f"DELETE FROM ProductCategoryLinks WHERE product_id=? AND category_id IN ({placeholders})",
                (pid, *category_ids),
            )
            deleted += cur.rowcount
        return deleted


def get_product_additional_categories(product_id: int) -> List[int]:
    with get_connection() as conn:
        return [int(row[0]) for row in conn.execute("SELECT category_id FROM ProductCategoryLinks WHERE product_id=?", (product_id,))]


def delete_product(product_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM Products WHERE id=?", (product_id,))
        conn.commit()


def get_product(product_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, sku, supplier_sku, name, brand_id, category_id, unit, is_active FROM Products WHERE id=?",
            (product_id,),
        ).fetchone()
        if not row:
            return None
        extra = [r[0] for r in conn.execute(
            "SELECT category_id FROM AdditionalProductCategories WHERE product_id=?", (product_id,)
        ).fetchall()]
    return {
        "id": row["id"],
        "sku": row["sku"],
        "supplier_sku": row["supplier_sku"],
        "name": row["name"],
        "brand_id": row["brand_id"],
        "category_id": row["category_id"],
        "unit": row["unit"],
        "is_active": bool(row["is_active"]),
        "extra_categories": extra,
    }


# Assemblies / component groups

def list_component_groups() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, code, name, note FROM ComponentGroups ORDER BY code").fetchall()
    return [dict(r) for r in rows]


def create_component_group(code: str, name: str, note: str | None = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO ComponentGroups (code, name, note) VALUES (?, ?, ?)",
            (code.strip(), name.strip(), (note or "").strip() or None),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_component_group(group_id: int, code: str, name: str, note: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE ComponentGroups SET code=?, name=?, note=? WHERE id=?",
            (code.strip(), name.strip(), (note or "").strip() or None, group_id),
        )
        conn.commit()


def delete_component_group(group_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM ComponentGroups WHERE id=?", (group_id,))
        conn.commit()


def list_assembly_slots() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, code, name, note FROM AssemblySlots ORDER BY code").fetchall()
    return [dict(r) for r in rows]


def create_assembly_slot(code: str, name: str, note: str | None = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO AssemblySlots (code, name, note) VALUES (?, ?, ?)",
            (code.strip(), name.strip(), (note or "").strip() or None),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_assembly_slot(slot_id: int, code: str, name: str, note: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE AssemblySlots SET code=?, name=?, note=? WHERE id=?",
            (code.strip(), name.strip(), (note or "").strip() or None, slot_id),
        )
        conn.commit()


def delete_assembly_slot(slot_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM AssemblySlots WHERE id=?", (slot_id,))
        conn.commit()


def list_product_component_groups(product_id: int) -> list[dict]:
    query = """
        SELECT cg.id AS group_id, cg.code, cg.name, m.priority
        FROM ProductComponentGroupMembers m
        JOIN ComponentGroups cg ON cg.id = m.group_id
        WHERE m.product_id = ?
        ORDER BY m.priority, cg.code
    """
    with get_connection() as conn:
        rows = conn.execute(query, (product_id,)).fetchall()
    return [dict(r) for r in rows]


def set_product_in_component_group(
    product_id: int, group_id: int, in_group: bool, priority: int = 100
) -> None:
    with get_connection() as conn:
        if in_group:
            conn.execute(
                """
                INSERT INTO ProductComponentGroupMembers (group_id, product_id, priority)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id, product_id) DO UPDATE SET priority=excluded.priority
                """,
                (group_id, product_id, int(priority)),
            )
        else:
            conn.execute(
                "DELETE FROM ProductComponentGroupMembers WHERE group_id=? AND product_id=?",
                (group_id, product_id),
            )
        conn.commit()


def get_product_group_priority(product_id: int, group_id: int) -> Optional[int]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT priority FROM ProductComponentGroupMembers WHERE product_id=? AND group_id=?",
            (product_id, group_id),
        ).fetchone()
    return int(row["priority"]) if row else None


def list_product_slot_coverage(product_id: int) -> list[dict]:
    query = """
        SELECT s.id AS slot_id, s.code, s.name
        FROM ProductSlotCoverage c
        JOIN AssemblySlots s ON s.id = c.slot_id
        WHERE c.product_id = ?
        ORDER BY s.code
    """
    with get_connection() as conn:
        rows = conn.execute(query, (product_id,)).fetchall()
    return [dict(r) for r in rows]


def set_product_slot_covered(product_id: int, slot_id: int, covered: bool) -> None:
    with get_connection() as conn:
        if covered:
            conn.execute(
                """
                INSERT OR IGNORE INTO ProductSlotCoverage (product_id, slot_id) VALUES (?, ?)
                """,
                (product_id, slot_id),
            )
        else:
            conn.execute(
                "DELETE FROM ProductSlotCoverage WHERE product_id=? AND slot_id=?",
                (product_id, slot_id),
            )
        conn.commit()


def list_all_groups_with_product_flag(product_id: int) -> list[dict]:
    groups = list_component_groups()
    memberships = {
        int(row["group_id"]): int(row.get("priority", 0) or 0)
        for row in list_product_component_groups(product_id)
    }
    result: list[dict] = []
    for group in groups:
        gid = int(group["id"])
        priority = memberships.get(gid)
        result.append(
            {
                "id": gid,
                "code": group.get("code"),
                "name": group.get("name"),
                "note": group.get("note"),
                "is_member": gid in memberships,
                "priority": priority,
            }
        )
    return result


def list_all_slots_with_product_flag(product_id: int) -> list[dict]:
    slots = list_assembly_slots()
    covered_ids = {row["slot_id"] for row in list_product_slot_coverage(product_id)}
    return [
        {
            "id": int(slot["id"]),
            "code": slot.get("code"),
            "name": slot.get("name"),
            "note": slot.get("note"),
            "is_covered": int(slot["id"]) in covered_ids,
        }
        for slot in slots
    ]


def list_product_requirements(product_id: int, conn: sqlite3.Connection | None = None) -> list[dict]:
    query = """
        SELECT r.slot_id,
               s.code AS slot_code,
               s.name AS slot_name,
               r.group_id,
               cg.code AS group_code,
               cg.name AS group_name,
               r.qty
        FROM ProductAssemblyRequirements r
        JOIN AssemblySlots s ON s.id = r.slot_id
        LEFT JOIN ComponentGroups cg ON cg.id = r.group_id
        WHERE r.product_id = ?
        ORDER BY s.code
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        rows = conn.execute(query, (product_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn and conn is not None:
            conn.close()


def upsert_product_requirement(
    product_id: int, slot_id: int, qty: float, group_id: int | None = None
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO ProductAssemblyRequirements (product_id, slot_id, group_id, qty)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(product_id, slot_id) DO UPDATE SET group_id=excluded.group_id, qty=excluded.qty
            """,
            (product_id, slot_id, group_id, float(qty)),
        )
        conn.commit()


def delete_product_requirement(product_id: int, slot_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM ProductAssemblyRequirements WHERE product_id=? AND slot_id=?", (product_id, slot_id))
        conn.commit()


def list_group_members(group_id: int, conn: sqlite3.Connection | None = None) -> list[dict]:
    query = """
        SELECT m.product_id, p.sku, p.name, m.priority
        FROM ProductComponentGroupMembers m
        JOIN Products p ON p.id = m.product_id
        WHERE m.group_id = ?
        ORDER BY m.priority, p.sku
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        rows = conn.execute(query, (group_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn and conn is not None:
            conn.close()


def list_product_coverage_slot_ids(product_id: int, conn: sqlite3.Connection | None = None) -> Set[int]:
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT slot_id FROM ProductSlotCoverage WHERE product_id=?", (product_id,)
        ).fetchall()
        return {int(r["slot_id"]) for r in rows}
    finally:
        if owns_conn and conn is not None:
            conn.close()


def list_products_covering_slots(
    slot_ids: Iterable[int], conn: sqlite3.Connection | None = None
) -> list[dict]:
    """Return products that cover any of the provided slots (used for unrestricted slots)."""
    slot_ids = list({int(sid) for sid in slot_ids})
    if not slot_ids:
        return []
    placeholders = ",".join("?" for _ in slot_ids)
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT c.product_id, p.sku, p.name, c.slot_id
            FROM ProductSlotCoverage c
            JOIN Products p ON p.id = c.product_id
            WHERE c.slot_id IN ({placeholders})
            """,
            tuple(slot_ids),
        ).fetchall()
    finally:
        if owns_conn and conn is not None:
            conn.close()
    products: Dict[int, dict] = {}
    for row in rows:
        pid = int(row["product_id"])
        entry = products.setdefault(
            pid,
            {"product_id": pid, "sku": row["sku"], "name": row["name"], "coverage": set()},
        )
        entry["coverage"].add(int(row["slot_id"]))
    return list(products.values())


def get_product_group_memberships(
    product_ids: Iterable[int], conn: sqlite3.Connection | None = None
) -> Dict[int, Dict[int, int]]:
    """Return mapping product_id -> {group_id: priority}."""
    ids = list({int(pid) for pid in product_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT product_id, group_id, priority FROM ProductComponentGroupMembers WHERE product_id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    finally:
        if owns_conn and conn is not None:
            conn.close()
    result: Dict[int, Dict[int, int]] = {}
    for row in rows:
        pid = int(row["product_id"])
        gid = int(row["group_id"])
        prio = int(row.get("priority") or 0)
        result.setdefault(pid, {})[gid] = prio
    return result


# Warehouses and channels

def list_warehouses(active_only: bool = False) -> List[sqlite3.Row]:
    query = "SELECT id, name, description, is_active FROM Warehouses"
    if active_only:
        query += " WHERE is_active=1"
    query += " ORDER BY name"
    with get_connection() as conn:
        return list(conn.execute(query))


def add_warehouse(name: str, description: str = "", is_active: bool = True) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO Warehouses (name, description, is_active) VALUES (?,?,?)", (name.strip(), description.strip(), 1 if is_active else 0)
        )
        conn.commit()
        return cur.lastrowid


def update_warehouse(warehouse_id: int, name: str, description: str = "", is_active: bool = True) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE Warehouses SET name=?, description=?, is_active=? WHERE id=?",
            (name.strip(), description.strip(), 1 if is_active else 0, warehouse_id),
        )
        conn.commit()


def delete_warehouse(warehouse_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM Warehouses WHERE id=?", (warehouse_id,))
        conn.commit()


def list_channels(active_only: bool = False) -> List[sqlite3.Row]:
    query = "SELECT id, name, is_active FROM SalesChannels"
    if active_only:
        query += " WHERE is_active=1"
    query += " ORDER BY name"
    with get_connection() as conn:
        return list(conn.execute(query))


def add_channel(name: str, is_active: bool = True) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO SalesChannels (name, is_active) VALUES (?, ?)",
            (name.strip(), 1 if is_active else 0),
        )
        conn.commit()
        return cur.lastrowid


def update_channel(channel_id: int, name: str, is_active: bool = True) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE SalesChannels SET name=?, is_active=? WHERE id=?",
            (name.strip(), 1 if is_active else 0, channel_id),
        )
        conn.commit()


def delete_channel(channel_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM SalesChannels WHERE id=?", (channel_id,))
        conn.commit()


# Currencies


def list_currencies(active_only: bool = True) -> List[sqlite3.Row]:
    query = "SELECT code, name, decimals, is_active FROM Currencies"
    if active_only:
        query += " WHERE is_active=1"
    query += " ORDER BY code"
    with get_connection() as conn:
        return list(conn.execute(query))


def add_currency(code: str, name: str, decimals: int = 2, is_active: bool = True) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO Currencies (code, name, decimals, is_active) VALUES (?,?,?,?)",
            (code.strip().upper(), name.strip(), max(decimals, 0), 1 if is_active else 0),
        )
        conn.commit()


def update_currency(code: str, name: str, decimals: int = 2, is_active: bool = True) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE Currencies SET name=?, decimals=?, is_active=? WHERE code=?",
            (name.strip(), max(decimals, 0), 1 if is_active else 0, code.strip().upper()),
        )
        conn.commit()


def delete_currency(code: str) -> None:
    code = code.strip().upper()
    if code == utils.get_base_currency_code():
        raise ValueError("Базову валюту не можна видалити")
    with get_connection() as conn:
        conn.execute("DELETE FROM Currencies WHERE code=?", (code,))
        conn.commit()


def add_currency_rate(currency_code: str, rate_date: str, rate: float) -> int:
    currency_code = currency_code.strip().upper()
    rate_date = _normalize_date(rate_date, field_label="Дата")
    if rate <= 0:
        raise ValueError("Курс має бути більшим за 0")
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM CurrencyRates WHERE currency_code=? AND rate_date=?",
            (currency_code, rate_date),
        ).fetchone()
        if exists:
            raise ValueError("Курс для цієї валюти на цю дату вже існує")
        cur = conn.execute(
            "INSERT INTO CurrencyRates (currency_code, rate_date, rate) VALUES (?,?,?)",
            (currency_code, rate_date, rate),
        )
        conn.commit()
        return cur.lastrowid


def update_currency_rate(rate_id: int, rate_date: str, rate: float) -> None:
    rate_date = _normalize_date(rate_date, field_label="Дата")
    if rate <= 0:
        raise ValueError("Курс має бути більшим за 0")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT currency_code FROM CurrencyRates WHERE id=?",
            (rate_id,),
        ).fetchone()
        if not row:
            raise ValueError("Курс не знайдено")
        currency_code = row[0]
        exists = conn.execute(
            "SELECT 1 FROM CurrencyRates WHERE currency_code=? AND rate_date=? AND id<>?",
            (currency_code, rate_date, rate_id),
        ).fetchone()
        if exists:
            raise ValueError("Курс для цієї валюти на цю дату вже існує")
        conn.execute(
            "UPDATE CurrencyRates SET rate_date=?, rate=? WHERE id=?",
            (rate_date, rate, rate_id),
        )
        conn.commit()


def rate_on_date(currency_code: str, rate_date: str) -> Optional[float]:
    currency_code = currency_code.strip().upper()
    rate_date = _normalize_date(rate_date, field_label="Дата")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT rate FROM CurrencyRates WHERE currency_code=? AND rate_date=? ORDER BY id DESC LIMIT 1",
            (currency_code, rate_date),
        ).fetchone()
    return float(row[0]) if row else None


def list_currency_rates(currency_code: Optional[str] = None) -> List[sqlite3.Row]:
    query = "SELECT id, currency_code, rate_date, rate FROM CurrencyRates"
    params: Tuple[str, ...] = ()
    if currency_code:
        query += " WHERE currency_code=?"
        params = (currency_code.strip().upper(),)
    query += " ORDER BY rate_date DESC, id DESC"
    with get_connection() as conn:
        return list(conn.execute(query, params))


def delete_currency_rate(rate_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM CurrencyRates WHERE id=?", (rate_id,))
        conn.commit()


def latest_rate(currency_code: str) -> float:
    currency_code = currency_code.strip().upper()
    if currency_code == utils.get_base_currency_code():
        return 1.0
    with get_connection() as conn:
        row = conn.execute(
            "SELECT rate FROM CurrencyRates WHERE currency_code=? ORDER BY rate_date DESC, id DESC LIMIT 1",
            (currency_code,),
        ).fetchone()
    if not row:
        raise ValueError(f"Немає курсу для {currency_code}")
    return float(row[0])


def rate_on_or_before(currency_code: str, rate_date: str) -> float:
    currency_code = currency_code.strip().upper()
    rate_date = _normalize_date(rate_date, field_label="Дата")
    if currency_code == utils.get_base_currency_code():
        return 1.0
    with get_connection() as conn:
        row = conn.execute(
            "SELECT rate FROM CurrencyRates WHERE currency_code=? AND rate_date<=? ORDER BY rate_date DESC, id DESC LIMIT 1",
            (currency_code, rate_date),
        ).fetchone()
        if row:
            return float(row[0])
        row = conn.execute(
            "SELECT rate FROM CurrencyRates WHERE currency_code=? ORDER BY rate_date DESC, id DESC LIMIT 1",
            (currency_code,),
        ).fetchone()
        if row:
            return float(row[0])
    raise ValueError(f"Немає курсу для {currency_code}")


# Counterparties

def list_counterparties(counterparty_type: Optional[str] = None) -> List[sqlite3.Row]:
    query = "SELECT id, name, type, phone, email, address, note FROM Counterparties"
    params: Tuple[str, ...] = ()
    if counterparty_type:
        query += " WHERE type = ?"
        params = (counterparty_type,)
    query += " ORDER BY name"
    with get_connection() as conn:
        return list(conn.execute(query, params))


def list_suppliers() -> list[sqlite3.Row]:
    query = "SELECT id, name FROM Counterparties WHERE type IN ('supplier','both') ORDER BY name"
    with get_connection() as conn:
        return list(conn.execute(query))

def list_customers() -> list[sqlite3.Row]:
    query = "SELECT id, name FROM Counterparties WHERE type IN ('customer','both') ORDER BY name"
    with get_connection() as conn:
        return list(conn.execute(query))


def add_counterparty(
    name: str, ctype: str, phone: str = "", email: str = "", address: str = "", note: str = ""
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO Counterparties (name, type, phone, email, address, note) VALUES (?,?,?,?,?,?)",
            (name.strip(), ctype, phone.strip(), email.strip(), address.strip(), note.strip()),
        )
        conn.commit()
        return cur.lastrowid


def update_counterparty(
    counterparty_id: int,
    name: str,
    ctype: str,
    phone: str = "",
    email: str = "",
    address: str = "",
    note: str = "",
) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE Counterparties SET name=?, type=?, phone=?, email=?, address=?, note=? WHERE id=?",
            (name.strip(), ctype, phone.strip(), email.strip(), address.strip(), note.strip(), counterparty_id),
        )
        conn.commit()


def delete_counterparty(counterparty_id: int) -> None:
    with get_connection() as conn:
        used_purchase = conn.execute(
            "SELECT COUNT(*) FROM PurchaseDocuments WHERE supplier_id=?", (counterparty_id,)
        ).fetchone()[0]
    with get_connection() as conn:
        used_sales = conn.execute("SELECT COUNT(*) FROM SalesDocuments WHERE customer_id=?", (counterparty_id,)).fetchone()[0]
        if used_purchase or used_sales:
            raise ValueError("Контрагент використовується у документах")
        conn.execute("DELETE FROM Counterparties WHERE id=?", (counterparty_id,))
        conn.commit()


# Helpers for balances


def ensure_import_defaults(brand_name: str = "Імпорт", category_name: str = "Імпорт") -> Tuple[int, int]:
    """Return (brand_id, category_id) ensuring preferred names exist.

    If the provided ``brand_name``/``category_name`` exists (case-insensitive), its id
    is returned. Otherwise the records are created, falling back to the first existing
    entry only when no preferred name is supplied.
    """

    def _find_id_casefold(conn: sqlite3.Connection, table: str, name: str) -> Optional[int]:
        target = name.casefold()
        for row in conn.execute(f"SELECT id, name FROM {table}"):
            if row["name"].casefold() == target:
                return int(row["id"])
        return None

    brand_name = (brand_name or "").strip()
    category_name = (category_name or "").strip()

    with get_connection() as conn:
        # Brand
        brand_id = _find_id_casefold(conn, "Brands", brand_name) if brand_name else None
        if brand_id is None:
            if not brand_name:
                fallback = conn.execute("SELECT id FROM Brands ORDER BY id LIMIT 1").fetchone()
                if fallback:
                    brand_id = int(fallback["id"])
                else:
                    conn.execute("INSERT OR IGNORE INTO Brands (name) VALUES (?)", ("Імпорт",))
                    conn.commit()
                    brand_id = _find_id_casefold(conn, "Brands", "Імпорт")
                    if brand_id is None:
                        raise RuntimeError("Не вдалося створити бренд за замовчуванням для імпорту")
            else:
                conn.execute("INSERT OR IGNORE INTO Brands (name) VALUES (?)", (brand_name,))
                conn.commit()
                brand_id = _find_id_casefold(conn, "Brands", brand_name)
                if brand_id is None:
                    raise RuntimeError("Не вдалося визначити бренд для імпорту")

        # Category
        category_id = _find_id_casefold(conn, "Categories", category_name) if category_name else None
        if category_id is None:
            if not category_name:
                fallback = conn.execute("SELECT id FROM Categories ORDER BY id LIMIT 1").fetchone()
                if fallback:
                    category_id = int(fallback["id"])
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO Categories (name, sort_order, is_service, is_hidden) VALUES (?, 0, 0, 0)",
                        ("Імпорт",),
                    )
                    conn.commit()
                    category_id = _find_id_casefold(conn, "Categories", "Імпорт")
                    if category_id is None:
                        raise RuntimeError("Не вдалося створити категорію за замовчуванням для імпорту")
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO Categories (name, sort_order, is_service, is_hidden) VALUES (?, 0, 0, 0)",
                    (category_name,),
                )
                conn.commit()
                category_id = _find_id_casefold(conn, "Categories", category_name)
                if category_id is None:
                    raise RuntimeError("Не вдалося визначити категорію для імпорту")

    return brand_id, category_id


def stock_on_hand(warehouse_id: int) -> Dict[int, float]:
    """Return mapping product_id -> available quantity for the warehouse."""

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT product_id, quantity FROM StockBalances WHERE warehouse_id=?", (warehouse_id,)
        ).fetchall()
    return {int(row["product_id"]): float(row["quantity"]) for row in rows}


def get_stock_quantity(
    product_id: int, warehouse_id: int, conn: sqlite3.Connection | None = None
) -> float:
    """Convenience wrapper to fetch current balance for a product in a warehouse."""

    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT quantity FROM StockBalances WHERE product_id=? AND warehouse_id=?",
            (product_id, warehouse_id),
        ).fetchone()
        return float(row[0]) if row else 0.0
    finally:
        if owns_conn and conn is not None:
            conn.close()


def get_stock_balance(product_id: int, warehouse_id: int) -> Tuple[float, float]:
    """Return current quantity and average cost for a product in a warehouse."""

    with get_connection() as conn:
        return _get_balance(conn, product_id, warehouse_id)


def find_counterparty_by_name(name: str, allowed_types: Optional[Sequence[str]] = None) -> Optional[sqlite3.Row]:
    """Find counterparty by name (case-insensitive) limited to types if provided."""

    allowed_types = tuple(allowed_types or [])
    with get_connection() as conn:
        base_query = "SELECT id, name, type, phone, email, address, note FROM Counterparties WHERE lower(name)=?"
        params: List[object] = [name.strip().lower()]
        if allowed_types:
            placeholders = ",".join("?" * len(allowed_types))
            base_query += f" AND type IN ({placeholders})"
            params.extend(allowed_types)
        return conn.execute(base_query, tuple(params)).fetchone()

def _get_balance(conn: sqlite3.Connection, product_id: int, warehouse_id: int) -> Tuple[float, float]:
    row = conn.execute(
        "SELECT quantity, average_cost FROM StockBalances WHERE product_id=? AND warehouse_id=?",
        (product_id, warehouse_id),
    ).fetchone()
    if not row:
        return 0.0, 0.0
    return float(row[0]), float(row[1])


def _set_balance(conn: sqlite3.Connection, product_id: int, warehouse_id: int, quantity: float, average_cost: float) -> None:
    conn.execute(
        "INSERT INTO StockBalances (product_id, warehouse_id, quantity, average_cost, updated_at) "
        "VALUES (?,?,?,?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(product_id, warehouse_id) DO UPDATE SET quantity=excluded.quantity, average_cost=excluded.average_cost, updated_at=CURRENT_TIMESTAMP",
        (product_id, warehouse_id, quantity, average_cost),
    )


def _get_last_purchase_price(
    conn: sqlite3.Connection, product_id: int, warehouse_id: int, doc_date: str
) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(NULLIF(pl.purchase_price_base, 0), pl.purchase_price) AS price
        FROM PurchaseLines pl
        JOIN PurchaseDocuments pd ON pd.id = pl.purchase_id
        WHERE pd.status='posted' AND pl.product_id=? AND pd.warehouse_id=? AND pd.doc_date <= ?
        ORDER BY pd.doc_date DESC, pd.id DESC, pl.id DESC
        LIMIT 1
        """,
        (product_id, warehouse_id, doc_date),
    ).fetchone()
    if not row:
        return 0.0
    try:
        return float(row[0] or 0.0)
    except (TypeError, ValueError):
        return 0.0


def get_last_purchase_price(product_id: int, warehouse_id: int, doc_date: str) -> float:
    with get_connection() as conn:
        return _get_last_purchase_price(conn, product_id, warehouse_id, doc_date)


def _document_total(conn: sqlite3.Connection, table: str, fk_field: str, doc_id: int) -> float:
    return float(
        conn.execute(f"SELECT IFNULL(SUM(amount),0) FROM {table} WHERE {fk_field}=?", (doc_id,)).fetchone()[0]
    )


def _validate_iso_date(doc_date: str) -> str:
    return _normalize_date(doc_date, field_label="Дата")


def _remove_cash_links(conn: sqlite3.Connection, doc_type: str, doc_id: int) -> None:
    conn.execute(
        "DELETE FROM CashTransactions WHERE related_doc_type=? AND related_doc_id=?",
        (doc_type, doc_id),
    )


# Purchases

def create_purchase(
    doc_date: str,
    supplier_id: Optional[int],
    warehouse_id: int,
    channel: str,
    comment: str = "",
    currency_code: Optional[str] = None,
    exchange_rate: float = 1.0,
) -> int:
    doc_date = _validate_iso_date(doc_date)
    currency_value = (currency_code or utils.get_base_currency_code()).strip().upper()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO PurchaseDocuments (doc_date, supplier_id, warehouse_id, channel, comment, status, currency_code, exchange_rate) "
            "VALUES (?,?,?,?,?, 'draft', ?, ?)",
            (doc_date, supplier_id, warehouse_id, channel.strip(), comment.strip(), currency_value, exchange_rate),
        )
        conn.commit()
        return cur.lastrowid


def update_purchase(
    purchase_id: int,
    doc_date: str,
    supplier_id: Optional[int],
    warehouse_id: int,
    channel: str,
    comment: str,
    currency_code: str,
    exchange_rate: float,
) -> None:
    doc_date = _validate_iso_date(doc_date)
    with get_connection() as conn:
        status = conn.execute("SELECT status FROM PurchaseDocuments WHERE id=?", (purchase_id,)).fetchone()
        if not status:
            raise ValueError("Документ не знайдено")
        if status[0] != "draft":
            raise ValueError("Редагування можливе лише у чернетці")
        conn.execute(
            "UPDATE PurchaseDocuments SET doc_date=?, supplier_id=?, warehouse_id=?, channel=?, comment=?, currency_code=?, exchange_rate=? WHERE id=?",
            (
                doc_date,
                supplier_id,
                warehouse_id,
                channel.strip(),
                comment.strip(),
                currency_code.strip().upper(),
                exchange_rate,
                purchase_id,
            ),
        )
        conn.commit()


def replace_purchase_lines(purchase_id: int, lines: Iterable[Tuple[int, float, float]], exchange_rate: float) -> None:
    with get_connection() as conn:
        status = conn.execute("SELECT status FROM PurchaseDocuments WHERE id=?", (purchase_id,)).fetchone()
        if not status or status[0] != "draft":
            raise ValueError("Рядки можна змінювати лише у чернетці")
        allocated = conn.execute(
            "SELECT COUNT(*) FROM ExtraCostAllocations eca JOIN PurchaseLines pl ON pl.id = eca.purchase_line_id WHERE pl.purchase_id=?",
            (purchase_id,),
        ).fetchone()[0]
        if allocated:
            raise ValueError("Спочатку відмініть супутні витрати, розподілені на цю закупівлю")
        conn.execute("DELETE FROM PurchaseLines WHERE purchase_id=?", (purchase_id,))
        for product_id, qty, price in lines:
            amount_doc = qty * price
            price_base = price * exchange_rate
            amount = qty * price_base
            conn.execute(
                "INSERT INTO PurchaseLines (purchase_id, product_id, quantity, purchase_price, amount_doc, purchase_price_base, amount) "
                "VALUES (?,?,?,?,?,?,?)",
                (purchase_id, product_id, qty, price, amount_doc, price_base, amount),
            )
        conn.commit()


def list_purchases(status: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[sqlite3.Row]:
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    query = (
        "SELECT p.id, p.doc_date, p.status, p.comment, p.channel, p.supplier_id, c.name as supplier, w.name as warehouse, p.currency_code, p.exchange_rate, "
        "IFNULL(SUM(pl.amount),0) as total, IFNULL(SUM(pl.amount_doc),0) as total_doc, IFNULL(SUM(pl.extra_cost_allocated_base),0) as total_extra_base "
        "FROM PurchaseDocuments p "
        "LEFT JOIN Counterparties c ON c.id = p.supplier_id "
        "LEFT JOIN Warehouses w ON w.id = p.warehouse_id "
        "LEFT JOIN PurchaseLines pl ON pl.purchase_id = p.id"
    )
    clauses: List[str] = []
    params: List[object] = []
    if status:
        clauses.append("p.status=?")
        params.append(status)
    if date_from:
        clauses.append("p.doc_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("p.doc_date <= ?")
        params.append(date_to)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " GROUP BY p.id ORDER BY p.doc_date, p.id"
    with get_connection() as conn:
        return list(conn.execute(query, params))


def get_purchase(purchase_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM PurchaseDocuments WHERE id=?", (purchase_id,)).fetchone()


def list_purchase_lines(purchase_id: int) -> List[sqlite3.Row]:
    with get_connection() as conn:
        return list(
            conn.execute(
                "SELECT pl.id, pl.product_id, pl.quantity, pl.purchase_price, pl.amount_doc, pl.purchase_price_base, pl.amount, pl.extra_cost_allocated_base, p.name as product_name, p.sku "
                "FROM PurchaseLines pl JOIN Products p ON p.id = pl.product_id WHERE pl.purchase_id=?",
                (purchase_id,),
            )
        )


# Extra cost documents


def create_extra_cost_document(
    doc_date: str,
    currency_code: Optional[str] = None,
    exchange_rate: float = 1.0,
    partner_id: Optional[int] = None,
    comment: str = "",
) -> int:
    doc_date = _validate_iso_date(doc_date)
    currency_value = (currency_code or utils.get_base_currency_code()).strip().upper()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO ExtraCostDocuments (doc_date, currency_code, exchange_rate, partner_id, status, comment) VALUES (?, ?, ?, ?, 'draft', ?)",
            (doc_date, currency_value, exchange_rate, partner_id, comment.strip()),
        )
        conn.commit()
        return cur.lastrowid


def update_extra_cost_document(
    doc_id: int,
    doc_date: str,
    currency_code: str,
    exchange_rate: float,
    partner_id: Optional[int],
    comment: str,
) -> None:
    doc_date = _validate_iso_date(doc_date)
    with get_connection() as conn:
        status = conn.execute("SELECT status FROM ExtraCostDocuments WHERE id=?", (doc_id,)).fetchone()
        if not status:
            raise ValueError("Документ не знайдено")
        if status[0] != "draft":
            raise ValueError("Редагування можливе лише у чернетці")
        conn.execute(
            "UPDATE ExtraCostDocuments SET doc_date=?, currency_code=?, exchange_rate=?, partner_id=?, comment=? WHERE id=?",
            (doc_date, currency_code.strip().upper(), exchange_rate, partner_id, comment.strip(), doc_id),
        )
        conn.commit()


def delete_extra_cost_document(doc_id: int) -> None:
    with get_connection() as conn:
        status = conn.execute("SELECT status FROM ExtraCostDocuments WHERE id=?", (doc_id,)).fetchone()
        if not status:
            return
        if status[0] != "draft":
            raise ValueError("Спочатку відмініть проведення документа")
        conn.execute("DELETE FROM ExtraCostLines WHERE extra_cost_id=?", (doc_id,))
        conn.execute("DELETE FROM ExtraCostDocuments WHERE id=?", (doc_id,))
        conn.commit()


def _recalc_extra_cost_totals(conn: sqlite3.Connection, doc_id: int) -> None:
    totals = conn.execute(
        "SELECT IFNULL(SUM(amount_doc),0) as total_doc, IFNULL(SUM(amount_base),0) as total_base FROM ExtraCostLines WHERE extra_cost_id=?",
        (doc_id,),
    ).fetchone()
    conn.execute(
        "UPDATE ExtraCostDocuments SET total_amount_doc=?, total_amount_base=? WHERE id=?",
        (totals["total_doc"], totals["total_base"], doc_id),
    )


def replace_extra_cost_lines(doc_id: int, lines: Iterable[Tuple[str, float]], exchange_rate: float) -> None:
    with get_connection() as conn:
        status = conn.execute("SELECT status FROM ExtraCostDocuments WHERE id=?", (doc_id,)).fetchone()
        if not status:
            raise ValueError("Документ не знайдено")
        if status[0] != "draft":
            raise ValueError("Рядки можна змінювати лише у чернетці")
        conn.execute("DELETE FROM ExtraCostLines WHERE extra_cost_id=?", (doc_id,))
        for cost_type, amount_doc in lines:
            amount_base = amount_doc * exchange_rate
            conn.execute(
                "INSERT INTO ExtraCostLines (extra_cost_id, cost_type, amount_doc, amount_base) VALUES (?,?,?,?)",
                (doc_id, cost_type.strip(), amount_doc, amount_base),
            )
        _recalc_extra_cost_totals(conn, doc_id)
        conn.commit()


def list_extra_cost_documents(
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    partner_id: Optional[int] = None,
) -> List[sqlite3.Row]:
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    query = (
        "SELECT e.id, e.doc_date, e.currency_code, e.exchange_rate, e.partner_id, e.status, e.total_amount_doc, e.total_amount_base, e.comment, c.name as partner "
        "FROM ExtraCostDocuments e "
        "LEFT JOIN Counterparties c ON c.id = e.partner_id"
    )
    clauses: List[str] = []
    params: List[object] = []
    if status:
        clauses.append("e.status=?")
        params.append(status)
    if date_from:
        clauses.append("e.doc_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("e.doc_date <= ?")
        params.append(date_to)
    if partner_id:
        clauses.append("e.partner_id=?")
        params.append(partner_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY e.doc_date, e.id"
    with get_connection() as conn:
        return list(conn.execute(query, params))


def get_extra_cost_document(doc_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM ExtraCostDocuments WHERE id=?", (doc_id,)).fetchone()


def list_extra_cost_lines(doc_id: int) -> List[sqlite3.Row]:
    with get_connection() as conn:
        return list(
            conn.execute(
                "SELECT id, cost_type, amount_doc, amount_base FROM ExtraCostLines WHERE extra_cost_id=?",
                (doc_id,),
            )
        )


def list_extra_cost_allocations(doc_id: int) -> List[sqlite3.Row]:
    with get_connection() as conn:
        return list(
            conn.execute(
                "SELECT eca.id, eca.purchase_line_id, eca.amount_allocated_base, pl.product_id, pl.quantity, pl.amount, pl.extra_cost_allocated_base, pl.purchase_id "
                "FROM ExtraCostAllocations eca JOIN PurchaseLines pl ON pl.id = eca.purchase_line_id WHERE eca.extra_cost_id=?",
                (doc_id,),
            )
        )


def _revert_extra_cost_allocations(conn: sqlite3.Connection, doc_id: int) -> None:
    allocations = list(
        conn.execute(
            "SELECT purchase_line_id, amount_allocated_base FROM ExtraCostAllocations WHERE extra_cost_id=?",
            (doc_id,),
        )
    )
    for alloc in allocations:
        conn.execute(
            "UPDATE PurchaseLines SET extra_cost_allocated_base = extra_cost_allocated_base - ? WHERE id=?",
            (alloc["amount_allocated_base"], alloc["purchase_line_id"]),
        )
    conn.execute("DELETE FROM ExtraCostCogsAllocations WHERE extra_cost_id=?", (doc_id,))
    conn.execute("DELETE FROM ExtraCostAllocations WHERE extra_cost_id=?", (doc_id,))
    conn.execute(
        "DELETE FROM StockMoves WHERE reference_type='extra_cost' AND reference_id=?",
        (doc_id,),
    )


def _allocate_extra_costs(conn: sqlite3.Connection, doc_id: int, purchase_ids: Sequence[int]) -> None:
    if not purchase_ids:
        raise ValueError("Не вибрано жодної закупівлі для розподілу")
    doc = conn.execute("SELECT * FROM ExtraCostDocuments WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        raise ValueError("Документ не знайдено")
    if doc["status"] != "posted":
        raise ValueError("Розподіл можливий лише для проведеного документа")
    extra_total_base = conn.execute(
        "SELECT IFNULL(SUM(amount_base),0) FROM ExtraCostLines WHERE extra_cost_id=?",
        (doc_id,),
    ).fetchone()[0]
    if extra_total_base <= 0:
        raise ValueError("Сума витрат повинна бути більшою за 0")
    placeholders = ",".join("?" for _ in purchase_ids)
    purchase_lines = list(
        conn.execute(
            f"SELECT pl.id, pl.amount as amount_base, pl.product_id, pl.quantity, pd.warehouse_id FROM PurchaseLines pl JOIN PurchaseDocuments pd ON pd.id = pl.purchase_id WHERE pl.purchase_id IN ({placeholders}) AND pd.status='posted'",
            purchase_ids,
        )
    )
    if not purchase_lines:
        raise ValueError("Немає рядків закупівель для розподілу")
    total_base = sum(float(ln["amount_base"]) for ln in purchase_lines)
    if total_base <= 0:
        raise ValueError("Немає бази для розподілу")
    _revert_extra_cost_allocations(conn, doc_id)
    for ln in purchase_lines:
        share = float(ln["amount_base"]) / total_base
        allocated = extra_total_base * share
        conn.execute(
            "INSERT INTO ExtraCostAllocations (extra_cost_id, purchase_line_id, amount_allocated_base) VALUES (?,?,?)",
            (doc_id, ln["id"], allocated),
        )
        conn.execute(
            "UPDATE PurchaseLines SET extra_cost_allocated_base = extra_cost_allocated_base + ? WHERE id=?",
            (allocated, ln["id"]),
        )


def allocate_extra_costs(
    doc_id: int, purchase_ids: Sequence[int], conn: sqlite3.Connection | None = None
) -> None:
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        if owns_conn:
            with transaction(conn):
                _allocate_extra_costs(conn, doc_id, purchase_ids)
        else:
            _allocate_extra_costs(conn, doc_id, purchase_ids)
    finally:
        if owns_conn:
            conn.close()


def post_extra_cost(doc_id: int, purchase_ids: Sequence[int]) -> None:
    if not purchase_ids:
        raise ValueError("Не вибрано жодної закупівлі")
    with get_connection() as conn:
        with transaction(conn):
            doc = conn.execute("SELECT * FROM ExtraCostDocuments WHERE id=?", (doc_id,)).fetchone()
            if not doc:
                raise ValueError("Документ не знайдено")
            status = (doc["status"] or "draft").strip()
            if status != "draft":
                raise ValueError("Документ вже проведено")
            has_lines = conn.execute("SELECT COUNT(*) FROM ExtraCostLines WHERE extra_cost_id=?", (doc_id,)).fetchone()[0]
            if not has_lines:
                raise ValueError("Немає рядків витрат")
            placeholders = ",".join("?" for _ in purchase_ids)
            max_purchase_date = conn.execute(
                f"SELECT MAX(doc_date) FROM PurchaseDocuments WHERE id IN ({placeholders}) AND status='posted'",
                purchase_ids,
            ).fetchone()[0]
            if max_purchase_date and doc["doc_date"] < max_purchase_date:
                raise ValueError(
                    "Дата витрат не може бути раніше дати закупівлі (Variant B). Змініть дату документа витрат або дату закупівлі."
                )
            _recalc_extra_cost_totals(conn, doc_id)
            totals = conn.execute(
                "SELECT total_amount_base FROM ExtraCostDocuments WHERE id=?", (doc_id,)
            ).fetchone()["total_amount_base"]
            if totals <= 0:
                raise ValueError("Сума витрат повинна бути більшою за 0")
            conn.execute("UPDATE ExtraCostDocuments SET status='posted' WHERE id=?", (doc_id,))
            _allocate_extra_costs(conn, doc_id, purchase_ids)
            recalc_stock(conn=conn)
            audit_event(
                "DOC_POST",
                "Extra cost posted",
                details={
                    "doc_date": doc["doc_date"],
                    "lines_count": int(has_lines),
                    "total_amount": float(totals or 0.0),
                },
                related_doc_type="extra_cost",
                related_doc_id=doc_id,
                conn=conn,
            )


def unpost_extra_cost(doc_id: int) -> None:
    with get_connection() as conn:
        with transaction(conn):
            doc = conn.execute(
                "SELECT status, doc_date FROM ExtraCostDocuments WHERE id=?", (doc_id,)
            ).fetchone()
            if not doc:
                raise ValueError("Документ не знайдено")
            status = (doc["status"] or "draft").strip()
            if status != "posted":
                raise ValueError("Документ не проведено")
            _revert_extra_cost_allocations(conn, doc_id)
            conn.execute("UPDATE ExtraCostDocuments SET status='draft' WHERE id=?", (doc_id,))
            recalc_stock(conn=conn)
            lines_count = conn.execute(
                "SELECT COUNT(*) FROM ExtraCostLines WHERE extra_cost_id=?", (doc_id,)
            ).fetchone()[0]
            audit_event(
                "DOC_UNPOST",
                "Extra cost unposted",
                details={"doc_date": doc["doc_date"], "lines_count": int(lines_count)},
                related_doc_type="extra_cost",
                related_doc_id=doc_id,
                conn=conn,
            )


# Sales

def create_sale(
    doc_date: str,
    customer_id: Optional[int],
    warehouse_id: int,
    channel: str,
    comment: str = "",
    currency_code: Optional[str] = None,
    exchange_rate: float = 1.0,
    order_expense_doc: float = 0.0,
) -> int:
    doc_date = _validate_iso_date(doc_date)
    currency_value = (currency_code or utils.get_base_currency_code()).strip().upper()
    with get_connection() as conn:
        order_expense_base = order_expense_doc * exchange_rate
        cur = conn.execute(
            "INSERT INTO SalesDocuments (doc_date, customer_id, warehouse_id, channel, comment, status, currency_code, exchange_rate, order_expense_doc, order_expense_base) "
            "VALUES (?,?,?,?,?, 'draft', ?, ?, ?, ?)",
            (
                doc_date,
                customer_id,
                warehouse_id,
                channel.strip(),
                comment.strip(),
                currency_value,
                exchange_rate,
                order_expense_doc,
                order_expense_base,
            ),
        )
        conn.commit()
        return cur.lastrowid


def update_sale(
    sale_id: int,
    doc_date: str,
    customer_id: Optional[int],
    warehouse_id: int,
    channel: str,
    comment: str,
    currency_code: str,
    exchange_rate: float,
    order_expense_doc: float = 0.0,
) -> None:
    doc_date = _validate_iso_date(doc_date)
    with get_connection() as conn:
        status = conn.execute("SELECT status FROM SalesDocuments WHERE id=?", (sale_id,)).fetchone()
        if not status:
            raise ValueError("Документ не знайдено")
        if status[0] != "draft":
            raise ValueError("Редагування можливе лише у чернетці")
        order_expense_base = order_expense_doc * exchange_rate
        conn.execute(
            "UPDATE SalesDocuments SET doc_date=?, customer_id=?, warehouse_id=?, channel=?, comment=?, currency_code=?, exchange_rate=?, order_expense_doc=?, order_expense_base=? WHERE id=?",
            (
                doc_date,
                customer_id,
                warehouse_id,
                channel.strip(),
                comment.strip(),
                currency_code.strip().upper(),
                exchange_rate,
                order_expense_doc,
                order_expense_base,
                sale_id,
            ),
        )
        conn.commit()


def replace_sale_lines(
    sale_id: int, lines: Iterable[tuple], exchange_rate: float, order_expense_doc: float = 0.0
) -> None:
    with get_connection() as conn:
        status = conn.execute("SELECT status FROM SalesDocuments WHERE id=?", (sale_id,)).fetchone()
        if not status or status[0] != "draft":
            raise ValueError("Рядки можна змінювати лише у чернетці")
        conn.execute("DELETE FROM SalesLines WHERE sale_id=?", (sale_id,))
        order_expense_base = order_expense_doc * exchange_rate
        amounts: List[float] = []
        lines_cache: List[Tuple[int, float, float, float, float]] = []
        for entry in lines:
            if len(entry) == 3:
                product_id, qty, price = entry
            elif len(entry) == 4:
                product_id, qty, price, _unit_expense_doc = entry
            else:
                raise ValueError("Невірний формат рядка продажу")
            amount_doc = qty * price
            price_base = price * exchange_rate
            amount = qty * price_base
            lines_cache.append((product_id, qty, price, amount_doc, price_base))
            amounts.append(amount)

        total_amount = sum(amounts)
        for idx, (product_id, qty, price, amount_doc, price_base) in enumerate(lines_cache):
            allocated_order_expense = (order_expense_base * amounts[idx] / total_amount) if total_amount else 0.0
            conn.execute(
                "INSERT INTO SalesLines (sale_id, product_id, quantity, sale_price, amount_doc, sale_price_base, amount, unit_expense_doc, unit_expense_base, order_expense_allocated_base) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    sale_id,
                    product_id,
                    qty,
                    price,
                    amount_doc,
                    price_base,
                    amount,
                    0.0,
                    0.0,
                    allocated_order_expense,
                ),
            )
        conn.commit()


def list_sales(status: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[sqlite3.Row]:
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    query = (
        "SELECT s.id, s.doc_date, s.status, s.comment, s.channel, s.customer_id, c.name as customer, w.name as warehouse, s.currency_code, s.exchange_rate, "
        "IFNULL(SUM(sl.amount),0) as total, IFNULL(SUM(sl.amount_doc),0) as total_doc "
        "FROM SalesDocuments s "
        "LEFT JOIN Counterparties c ON c.id = s.customer_id "
        "LEFT JOIN Warehouses w ON w.id = s.warehouse_id "
        "LEFT JOIN SalesLines sl ON sl.sale_id = s.id"
    )
    clauses: List[str] = []
    params: List[object] = []
    if status:
        clauses.append("s.status=?")
        params.append(status)
    if date_from:
        clauses.append("s.doc_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("s.doc_date <= ?")
        params.append(date_to)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " GROUP BY s.id ORDER BY s.doc_date, s.id"
    with get_connection() as conn:
        return list(conn.execute(query, params))


def get_sale(sale_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM SalesDocuments WHERE id=?", (sale_id,)).fetchone()


def list_sale_lines(sale_id: int) -> List[sqlite3.Row]:
    with get_connection() as conn:
        return list(
            conn.execute(
                "SELECT sl.id, sl.product_id, sl.quantity, sl.sale_price, sl.amount_doc, sl.sale_price_base, sl.amount, sl.unit_expense_doc, sl.order_expense_allocated_base, p.name as product_name, p.sku "
                "FROM SalesLines sl JOIN Products p ON p.id = sl.product_id WHERE sl.sale_id=?",
                (sale_id,),
            )
        )


def get_sale_assembly_plan_by_line(
    line_id: int, conn: sqlite3.Connection | None = None
) -> Optional[dict]:
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        plan_row = conn.execute(
            "SELECT id, sale_id, sale_line_id, parent_product_id, warehouse_id, parent_qty, created_at "
            "FROM SaleAssemblyPlans WHERE sale_line_id=?",
            (line_id,),
        ).fetchone()
        if not plan_row:
            return None
        lines = conn.execute(
            "SELECT id, plan_id, component_product_id, qty, cost_per_unit, amount, details_json "
            "FROM SaleAssemblyPlanLines WHERE plan_id=? ORDER BY id",
            (plan_row["id"],),
        ).fetchall()
        return {
            "id": plan_row["id"],
            "sale_id": plan_row["sale_id"],
            "sale_line_id": plan_row["sale_line_id"],
            "parent_product_id": plan_row["parent_product_id"],
            "warehouse_id": plan_row["warehouse_id"],
            "parent_qty": float(plan_row["parent_qty"] or 0.0),
            "created_at": plan_row["created_at"],
            "lines": [
                {
                    "id": ln["id"],
                    "plan_id": ln["plan_id"],
                    "component_product_id": ln["component_product_id"],
                    "qty": float(ln["qty"] or 0.0),
                    "cost_per_unit": float(ln["cost_per_unit"] or 0.0),
                    "amount": float(ln["amount"] or 0.0),
                    "details_json": ln["details_json"],
                }
                for ln in lines
            ],
        }
    finally:
        if owns_conn and conn is not None:
            conn.close()


def list_sale_assembly_plans(
    sale_id: int, conn: sqlite3.Connection | None = None
) -> list[dict]:
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        plan_rows = conn.execute(
            "SELECT id, sale_id, sale_line_id, parent_product_id, warehouse_id, parent_qty, created_at "
            "FROM SaleAssemblyPlans WHERE sale_id=? ORDER BY id",
            (sale_id,),
        ).fetchall()
        if not plan_rows:
            return []
        plan_ids = [int(r["id"]) for r in plan_rows]
        placeholders = ",".join("?" for _ in plan_ids)
        line_rows = conn.execute(
            f"SELECT id, plan_id, component_product_id, qty, cost_per_unit, amount, details_json "
            f"FROM SaleAssemblyPlanLines WHERE plan_id IN ({placeholders}) ORDER BY id",
            tuple(plan_ids),
        ).fetchall()
        lines_by_plan: Dict[int, list[dict]] = {}
        for ln in line_rows:
            lines_by_plan.setdefault(int(ln["plan_id"]), []).append(
                {
                    "id": ln["id"],
                    "plan_id": ln["plan_id"],
                    "component_product_id": ln["component_product_id"],
                    "qty": float(ln["qty"] or 0.0),
                    "cost_per_unit": float(ln["cost_per_unit"] or 0.0),
                    "amount": float(ln["amount"] or 0.0),
                    "details_json": ln["details_json"],
                }
            )
        return [
            {
                "id": pr["id"],
                "sale_id": pr["sale_id"],
                "sale_line_id": pr["sale_line_id"],
                "parent_product_id": pr["parent_product_id"],
                "warehouse_id": pr["warehouse_id"],
                "parent_qty": float(pr["parent_qty"] or 0.0),
                "created_at": pr["created_at"],
                "lines": lines_by_plan.get(int(pr["id"]), []),
            }
            for pr in plan_rows
        ]
    finally:
        if owns_conn and conn is not None:
            conn.close()


def delete_sale_assembly_plans_for_sale(
    sale_id: int, conn: sqlite3.Connection | None = None
) -> None:
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        with safe_transaction(conn):
            conn.execute("DELETE FROM SaleAssemblyPlans WHERE sale_id=?", (sale_id,))
    finally:
        if owns_conn and conn is not None:
            conn.close()


def create_sale_assembly_plan(
    sale_id: int,
    sale_line_id: int,
    parent_product_id: int,
    warehouse_id: int,
    parent_qty: float,
    components: list[dict],
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        with safe_transaction(conn):
            cur = conn.execute(
                """
                INSERT INTO SaleAssemblyPlans (sale_id, sale_line_id, parent_product_id, warehouse_id, parent_qty)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sale_id, sale_line_id, parent_product_id, warehouse_id, parent_qty),
            )
            plan_id = int(cur.lastrowid)
            for comp in components:
                conn.execute(
                    """
                    INSERT INTO SaleAssemblyPlanLines (plan_id, component_product_id, qty, cost_per_unit, amount, details_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan_id,
                        comp["component_product_id"],
                        float(comp.get("qty", 0.0) or 0.0),
                        float(comp.get("cost_per_unit", 0.0) or 0.0),
                        float(comp.get("amount", 0.0) or 0.0),
                        comp.get("details_json"),
                    ),
                )
            return plan_id
    finally:
        if owns_conn and conn is not None:
            conn.close()


def sale_line_has_assembly(
    line_id: int, conn: sqlite3.Connection | None = None
) -> dict:
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        plan_exists = conn.execute(
            "SELECT 1 FROM SaleAssemblyPlans WHERE sale_line_id=?",
            (line_id,),
        ).fetchone()
        prod_row = conn.execute("SELECT product_id FROM SalesLines WHERE id=?", (line_id,)).fetchone()
        product_id = int(prod_row["product_id"]) if prod_row else None
        requirements_exist = False
        if product_id is not None:
            req = conn.execute(
                "SELECT 1 FROM ProductAssemblyRequirements WHERE product_id=? LIMIT 1",
                (product_id,),
            ).fetchone()
            requirements_exist = bool(req)
        return {
            "plan_exists": bool(plan_exists),
            "requirements_exist": requirements_exist,
        }
    finally:
        if owns_conn and conn is not None:
            conn.close()


# Posting and stock movements

def _apply_purchase_line(conn: sqlite3.Connection, move_date: str, purchase_id: int, line: sqlite3.Row) -> None:
    qty = float(line["quantity"])
    base_price = line["purchase_price_base"] if "purchase_price_base" in line.keys() else None
    price = float(base_price if base_price is not None else line["purchase_price"])
    product_id = int(line["product_id"])
    warehouse_id = int(line["warehouse_id"])
    old_qty, old_avg = _get_balance(conn, product_id, warehouse_id)
    new_qty = old_qty + qty
    new_avg = (old_qty * old_avg + qty * price) / new_qty if new_qty else 0
    _set_balance(conn, product_id, warehouse_id, new_qty, new_avg)
    conn.execute(
        "INSERT INTO StockMoves (move_date, product_id, warehouse_id, qty_in, qty_out, cost_per_unit, amount, reference_type, reference_id, channel, counterparty_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (move_date, product_id, warehouse_id, qty, 0, price, qty * price, "purchase", purchase_id, line["channel"], line["supplier_id"]),
    )


def _stock_shortage_message(conn: sqlite3.Connection, product_id: int, warehouse_id: int) -> str:
    product = conn.execute("SELECT sku, name FROM Products WHERE id=?", (product_id,)).fetchone()
    warehouse = conn.execute("SELECT name FROM Warehouses WHERE id=?", (warehouse_id,)).fetchone()
    sku = product["sku"] if product else str(product_id)
    name = product["name"] if product else str(product_id)
    warehouse_name = warehouse["name"] if warehouse else str(warehouse_id)
    return f"Недостатньо залишку: SKU={sku}, Товар={name}, Склад={warehouse_name}"


def _apply_sale_line(conn: sqlite3.Connection, move_date: str, sale_id: int, line: sqlite3.Row, allow_negative: bool) -> None:
    qty = float(line["quantity"])
    product_id = int(line["product_id"])
    warehouse_id = int(line["warehouse_id"])
    current_qty, avg_cost = _get_balance(conn, product_id, warehouse_id)
    if qty > current_qty and not allow_negative:
        raise ValueError(_stock_shortage_message(conn, product_id, warehouse_id))
    new_qty = current_qty - qty
    _set_balance(conn, product_id, warehouse_id, new_qty, avg_cost)
    amount = -qty * avg_cost
    conn.execute(
        "INSERT INTO StockMoves (move_date, product_id, warehouse_id, qty_in, qty_out, cost_per_unit, amount, reference_type, reference_id, channel, counterparty_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (move_date, product_id, warehouse_id, 0, qty, avg_cost, amount, "sale", sale_id, line["channel"], line["customer_id"]),
    )


def _apply_sale_component_line(
    conn: sqlite3.Connection,
    move_date: str,
    sale_id: int,
    warehouse_id: int,
    component: dict,
    channel: str,
    customer_id: int | None,
    allow_negative: bool,
) -> None:
    """Write-off a component item for an assembly sale plan."""
    qty = float(component.get("qty", 0.0) or 0.0)
    product_id = int(component["component_product_id"])
    current_qty, avg_cost = _get_balance(conn, product_id, warehouse_id)
    if qty > current_qty and not allow_negative:
        raise ValueError(_stock_shortage_message(conn, product_id, warehouse_id))
    new_qty = current_qty - qty
    _set_balance(conn, product_id, warehouse_id, new_qty, avg_cost)
    cost_per_unit = float(component.get("cost_per_unit", avg_cost))
    amount = -qty * cost_per_unit
    conn.execute(
        "INSERT INTO StockMoves (move_date, product_id, warehouse_id, qty_in, qty_out, cost_per_unit, amount, reference_type, reference_id, channel, counterparty_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            move_date,
            product_id,
            warehouse_id,
            0,
            qty,
            cost_per_unit,
            amount,
            "sale_component",
            sale_id,
            channel,
            customer_id,
        ),
    )


def _recalc_stock(conn: sqlite3.Connection, allow_negative: bool) -> None:
    conn.execute("DELETE FROM StockBalances")
    conn.execute("DELETE FROM StockMoves")
    conn.execute("DELETE FROM ExtraCostCogsAllocations")
    conn.execute("UPDATE PurchaseLines SET extra_cost_allocated_base=0")

    eps = 1e-9
    lot_queues: Dict[Tuple[int, int], deque[Tuple[int, float]]] = {}
    initial_qty: Dict[int, float] = {}
    remaining_qty: Dict[int, float] = {}

    def _clamp(value: float, min_value: float, max_value: float) -> float:
        return max(min_value, min(max_value, value))

    def fifo_consume(pair: Tuple[int, int], qty: float, allow_negative: bool) -> None:
        queue = lot_queues.get(pair)
        remaining_to_consume = qty
        while remaining_to_consume > eps:
            if not queue:
                if allow_negative:
                    return
                raise ValueError(_stock_shortage_message(conn, pair[0], pair[1]))
            purchase_line_id, lot_qty = queue[0]
            take_qty = min(lot_qty, remaining_to_consume)
            lot_qty -= take_qty
            remaining_to_consume -= take_qty
            lot_qty = 0.0 if lot_qty < eps else lot_qty
            remaining_qty[purchase_line_id] = lot_qty
            if lot_qty <= eps:
                queue.popleft()
            else:
                queue[0] = (purchase_line_id, lot_qty)

    purchase_docs = conn.execute(
        "SELECT p.id, p.doc_date, p.supplier_id, p.warehouse_id, p.channel FROM PurchaseDocuments p WHERE p.status='posted'",
    ).fetchall()
    extra_docs = conn.execute(
        "SELECT id, doc_date, partner_id FROM ExtraCostDocuments WHERE status='posted'",
    ).fetchall()
    sales_docs = conn.execute(
        "SELECT s.id, s.doc_date, s.customer_id, s.warehouse_id, s.channel FROM SalesDocuments s WHERE s.status='posted'",
    ).fetchall()
    inventory_docs = conn.execute(
        "SELECT id, doc_date, warehouse_id, comment FROM InventoryDocuments WHERE status='posted'",
    ).fetchall()

    order_rank = {"purchase": 0, "extra": 1, "sale": 2, "inventory": 99}
    events: List[Tuple[str, sqlite3.Row]] = [
        ("purchase", doc) for doc in purchase_docs
    ] + [
        ("extra", doc) for doc in extra_docs
    ] + [
        ("sale", doc) for doc in sales_docs
    ] + [
        ("inventory", doc) for doc in inventory_docs
    ]
    events.sort(key=lambda item: (item[1]["doc_date"], order_rank[item[0]], item[1]["id"]))

    for etype, doc in events:
        if etype == "purchase":
            lines = conn.execute(
                "SELECT pl.id as purchase_line_id, pl.product_id, pl.quantity, pl.purchase_price, pl.purchase_price_base, ? as warehouse_id, ? as channel, ? as supplier_id FROM PurchaseLines pl WHERE pl.purchase_id=?",
                (doc["warehouse_id"], doc["channel"], doc["supplier_id"], doc["id"]),
            ).fetchall()
            for ln in lines:
                _apply_purchase_line(conn, doc["doc_date"], doc["id"], ln)
                qty = float(ln["quantity"])
                purchase_line_id = int(ln["purchase_line_id"])
                pair = (int(ln["product_id"]), int(ln["warehouse_id"]))
                lot_queues.setdefault(pair, deque()).append((purchase_line_id, qty))
                initial_qty[purchase_line_id] = qty
                remaining_qty[purchase_line_id] = qty
        elif etype == "extra":
            allocations = conn.execute(
                "SELECT eca.amount_allocated_base, pl.id as purchase_line_id, pl.product_id, pl.quantity as purchase_qty, pd.warehouse_id "
                "FROM ExtraCostAllocations eca "
                "JOIN PurchaseLines pl ON pl.id = eca.purchase_line_id "
                "JOIN PurchaseDocuments pd ON pd.id = pl.purchase_id "
                "WHERE eca.extra_cost_id=?",
                (doc["id"],),
            ).fetchall()
            per_pair: Dict[Tuple[int, int], float] = {}
            for alloc in allocations:
                conn.execute(
                    "UPDATE PurchaseLines SET extra_cost_allocated_base = extra_cost_allocated_base + ? WHERE id=?",
                    (alloc["amount_allocated_base"], alloc["purchase_line_id"]),
                )
                purchase_line_id = int(alloc["purchase_line_id"])
                init_qty = initial_qty.get(purchase_line_id, float(alloc["purchase_qty"] or 0.0))
                if purchase_line_id not in initial_qty:
                    initial_qty[purchase_line_id] = init_qty
                rem_qty = remaining_qty.get(purchase_line_id, init_qty)
                rem_qty = _clamp(rem_qty, 0.0, init_qty)
                sold_qty = max(0.0, init_qty - rem_qty)
                allocation_amount = float(alloc["amount_allocated_base"])
                to_cogs = allocation_amount * (sold_qty / init_qty) if init_qty > eps else 0.0
                to_stock = allocation_amount - to_cogs
                if to_cogs > eps and sold_qty > eps:
                    conn.execute(
                        "INSERT INTO ExtraCostCogsAllocations (extra_cost_id, purchase_line_id, product_id, warehouse_id, qty_sold, amount_cogs_base) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            doc["id"],
                            purchase_line_id,
                            alloc["product_id"],
                            alloc["warehouse_id"],
                            sold_qty,
                            to_cogs,
                        ),
                    )
                if to_stock > eps:
                    key = (alloc["product_id"], alloc["warehouse_id"])
                    per_pair[key] = per_pair.get(key, 0.0) + to_stock
            for (product_id, warehouse_id), extra_amount in per_pair.items():
                current_qty, current_avg = _get_balance(conn, product_id, warehouse_id)
                if current_qty <= eps:
                    continue
                new_avg = (current_qty * current_avg + extra_amount) / current_qty
                _set_balance(conn, product_id, warehouse_id, current_qty, new_avg)
                conn.execute(
                    "INSERT INTO StockMoves (move_date, product_id, warehouse_id, qty_in, qty_out, cost_per_unit, amount, reference_type, reference_id, channel, counterparty_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (doc["doc_date"], product_id, warehouse_id, 0, 0, new_avg, extra_amount, "extra_cost", doc["id"], "", doc["partner_id"]),
                )
        elif etype == "sale":
            lines = conn.execute(
                "SELECT sl.id as sale_line_id, sl.product_id, sl.quantity, sl.sale_price, sl.sale_price_base, ? as warehouse_id, ? as channel, ? as customer_id FROM SalesLines sl WHERE sl.sale_id=?",
                (doc["warehouse_id"], doc["channel"], doc["customer_id"], doc["id"]),
            ).fetchall()
            for ln in lines:
                warehouse_id = int(ln["warehouse_id"])
                product_id = int(ln["product_id"])
                sale_line_id = int(ln["sale_line_id"])

                plan = get_sale_assembly_plan_by_line(sale_line_id, conn=conn)
                if plan is None:
                    requirements = list_product_requirements(product_id, conn=conn)
                    if requirements:
                        # Freeze component selection per sale line once and reuse it on subsequent recalculations.
                        result = assembly_resolver.resolve_components_for_product(
                            product_id,
                            warehouse_id,
                            float(ln["quantity"]),
                            conn=conn,
                            allow_negative=allow_negative,
                        )
                        missing_slots = result.get("missing_slots") or []
                        if missing_slots:
                            missing_label = ", ".join(
                                f"{ms.get('slot_code') or ms.get('slot_name') or ms.get('missing_qty')}: {ms.get('missing_qty')}"
                                for ms in missing_slots
                            )
                            raise ValueError(f"Не вистачає компонентів для продажу #{doc['id']}: {missing_label}")
                        plan_components: list[dict] = []
                        for comp in result.get("components") or []:
                            comp_id = int(comp["product_id"])
                            comp_qty = float(comp.get("qty", 0.0) or 0.0)
                            _, avg_cost = _get_balance(conn, comp_id, warehouse_id)
                            details = comp.get("covered_slots") or []
                            plan_components.append(
                                {
                                    "component_product_id": comp_id,
                                    "qty": comp_qty,
                                    "cost_per_unit": avg_cost,
                                    "amount": comp_qty * avg_cost,
                                    "details_json": json.dumps({"covered_slots": details}, ensure_ascii=False)
                                    if details
                                    else None,
                                }
                            )
                        create_sale_assembly_plan(
                            doc["id"],
                            sale_line_id,
                            product_id,
                            warehouse_id,
                            float(ln["quantity"]),
                            plan_components,
                            conn=conn,
                        )
                        plan = get_sale_assembly_plan_by_line(sale_line_id, conn=conn)

                if plan and plan.get("lines"):
                    for comp in plan["lines"]:
                        pair = (int(comp["component_product_id"]), warehouse_id)
                        fifo_consume(pair, float(comp.get("qty") or 0.0), allow_negative)
                        _apply_sale_component_line(
                            conn,
                            doc["doc_date"],
                            doc["id"],
                            warehouse_id,
                            comp,
                            doc["channel"],
                            doc["customer_id"],
                            allow_negative,
                        )
                else:
                    pair = (product_id, warehouse_id)
                    fifo_consume(pair, float(ln["quantity"]), allow_negative)
                    _apply_sale_line(conn, doc["doc_date"], doc["id"], ln, allow_negative)
        elif etype == "inventory":
            lines = conn.execute(
                "SELECT product_id, counted_qty, cost_override FROM InventoryLines WHERE inventory_id=?",
                (doc["id"],),
            ).fetchall()
            for ln in lines:
                product_id = int(ln["product_id"])
                current_qty, avg_cost = _get_balance(conn, product_id, doc["warehouse_id"])
                target = float(ln["counted_qty"] or 0.0)
                diff = target - current_qty
                if abs(diff) < eps:
                    _set_balance(conn, product_id, doc["warehouse_id"], target, avg_cost)
                    continue
                if diff < 0:
                    qty_out = -diff
                    amount = -qty_out * avg_cost
                    _set_balance(conn, product_id, doc["warehouse_id"], target, avg_cost)
                    conn.execute(
                        "INSERT INTO StockMoves (move_date, product_id, warehouse_id, qty_in, qty_out, cost_per_unit, amount, reference_type, reference_id, channel, counterparty_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            doc["doc_date"],
                            product_id,
                            doc["warehouse_id"],
                            0,
                            qty_out,
                            avg_cost,
                            amount,
                            "inventory",
                            doc["id"],
                            "",
                            None,
                        ),
                    )
                else:
                    qty_in = diff
                    cost = _inventory_in_cost(
                        conn,
                        product_id,
                        doc["warehouse_id"],
                        doc["doc_date"],
                        current_qty,
                        avg_cost,
                        ln["cost_override"],
                    )
                    amount = qty_in * cost
                    new_avg = (current_qty * avg_cost + qty_in * cost) / target if target > eps else 0.0
                    _set_balance(conn, product_id, doc["warehouse_id"], target, new_avg)
                    conn.execute(
                        "INSERT INTO StockMoves (move_date, product_id, warehouse_id, qty_in, qty_out, cost_per_unit, amount, reference_type, reference_id, channel, counterparty_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            doc["doc_date"],
                            product_id,
                            doc["warehouse_id"],
                            qty_in,
                            0,
                            cost,
                            amount,
                            "inventory",
                            doc["id"],
                            "",
                            None,
                        ),
                    )


def recalc_stock(allow_negative: bool = False, conn: sqlite3.Connection | None = None) -> None:
    """Rebuild stock balances and stock moves from posted documents."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        if owns_conn:
            with transaction(conn):
                _recalc_stock(conn, allow_negative)
        else:
            _recalc_stock(conn, allow_negative)
    finally:
        if owns_conn:
            conn.close()

def post_purchase(purchase_id: int) -> None:
    with get_connection() as conn:
        with transaction(conn):
            doc_row = conn.execute(
                "SELECT status, supplier_id, doc_date, warehouse_id, channel FROM PurchaseDocuments WHERE id=?",
                (purchase_id,),
            ).fetchone()
            if not doc_row:
                raise ValueError("Документ не знайдено")
            if doc_row["status"] != "draft":
                raise ValueError("Документ вже проведено")
            has_lines = conn.execute("SELECT COUNT(*) FROM PurchaseLines WHERE purchase_id=?", (purchase_id,)).fetchone()[0]
            if not has_lines:
                raise ValueError("Немає рядків для проведення")
            conn.execute("UPDATE PurchaseDocuments SET status='posted' WHERE id=?", (purchase_id,))
            total = _document_total(conn, "PurchaseLines", "purchase_id", purchase_id)
            _remove_cash_links(conn, "purchase", purchase_id)
            if total:
                conn.execute(
                    "INSERT INTO CashTransactions (date, amount, type, counterparty_id, related_doc_type, related_doc_id, channel, comment) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc_row["doc_date"],
                        -abs(total),
                        "purchase_payment",
                        doc_row["supplier_id"],
                        "purchase",
                        purchase_id,
                        "",
                        "Оплата за закупівлю",
                    ),
                )
            recalc_stock(conn=conn)
            audit_event(
                "DOC_POST",
                "Purchase posted",
                details={
                    "doc_date": doc_row["doc_date"],
                    "warehouse_id": doc_row["warehouse_id"],
                    "lines_count": int(has_lines),
                    "total_amount": float(total or 0.0),
                },
                related_doc_type="purchase",
                related_doc_id=purchase_id,
                conn=conn,
            )


def unpost_purchase(purchase_id: int) -> None:
    with get_connection() as conn:
        with transaction(conn):
            doc_row = conn.execute(
                "SELECT status, doc_date, warehouse_id FROM PurchaseDocuments WHERE id=?", (purchase_id,)
            ).fetchone()
            if not doc_row:
                raise ValueError("Документ не знайдено")
            if doc_row["status"] != "posted":
                raise ValueError("Документ не проведено")
            blocking = conn.execute(
                """
                SELECT ecd.id, ecd.doc_date, ecd.total_amount_base
                FROM ExtraCostAllocations eca
                JOIN PurchaseLines pl ON pl.id = eca.purchase_line_id
                JOIN ExtraCostDocuments ecd ON ecd.id = eca.extra_cost_id
                WHERE pl.purchase_id=? AND ecd.status='posted'
                GROUP BY ecd.id, ecd.doc_date, ecd.total_amount_base
                ORDER BY ecd.doc_date, ecd.id
                """,
                (purchase_id,),
            ).fetchall()
            if blocking:
                details = ", ".join(
                    [f"#{row['id']} від {row['doc_date']} (сума {row['total_amount_base']})" for row in blocking]
                )
                raise ValueError(
                    "Не можна розпровести закупівлю, бо є проведені супутні витрати: " + details
                )
            conn.execute("UPDATE PurchaseDocuments SET status='draft' WHERE id=?", (purchase_id,))
            _remove_cash_links(conn, "purchase", purchase_id)
            recalc_stock(conn=conn)
            lines_count = conn.execute("SELECT COUNT(*) FROM PurchaseLines WHERE purchase_id=?", (purchase_id,)).fetchone()[0]
            audit_event(
                "DOC_UNPOST",
                "Purchase unposted",
                details={
                    "doc_date": doc_row["doc_date"],
                    "warehouse_id": doc_row["warehouse_id"],
                    "lines_count": int(lines_count),
                },
                related_doc_type="purchase",
                related_doc_id=purchase_id,
                conn=conn,
            )


def post_sale(sale_id: int, allow_negative: bool = False) -> None:
    with get_connection() as conn:
        with transaction(conn):
            doc_row = conn.execute(
                "SELECT status, customer_id, doc_date, channel, warehouse_id FROM SalesDocuments WHERE id=?",
                (sale_id,),
            ).fetchone()
            if not doc_row:
                raise ValueError("Документ не знайдено")
            if doc_row["status"] != "draft":
                raise ValueError("Документ вже проведено")
            has_lines = conn.execute("SELECT COUNT(*) FROM SalesLines WHERE sale_id=?", (sale_id,)).fetchone()[0]
            if not has_lines:
                raise ValueError("Немає рядків для проведення")
            conn.execute("UPDATE SalesDocuments SET status='posted' WHERE id=?", (sale_id,))
            total = _document_total(conn, "SalesLines", "sale_id", sale_id)
            _remove_cash_links(conn, "sale", sale_id)
            if total:
                conn.execute(
                    "INSERT INTO CashTransactions (date, amount, type, counterparty_id, related_doc_type, related_doc_id, channel, comment) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc_row["doc_date"],
                        abs(total),
                        "sale_payment",
                        doc_row["customer_id"],
                        "sale",
                        sale_id,
                        doc_row["channel"] or "",
                        "Оплата від клієнта",
                    ),
                )
            recalc_stock(allow_negative=allow_negative, conn=conn)
            audit_event(
                "DOC_POST",
                "Sale posted",
                details={
                    "doc_date": doc_row["doc_date"],
                    "warehouse_id": doc_row["warehouse_id"],
                    "lines_count": int(has_lines),
                    "total_amount": float(total or 0.0),
                },
                related_doc_type="sale",
                related_doc_id=sale_id,
                conn=conn,
            )


def unpost_sale(sale_id: int) -> None:
    with get_connection() as conn:
        with transaction(conn):
            doc_row = conn.execute(
                "SELECT status, doc_date, warehouse_id FROM SalesDocuments WHERE id=?", (sale_id,)
            ).fetchone()
            if not doc_row:
                raise ValueError("Документ не знайдено")
            if doc_row["status"] != "posted":
                raise ValueError("Документ не проведено")
            conn.execute("UPDATE SalesDocuments SET status='draft' WHERE id=?", (sale_id,))
            _remove_cash_links(conn, "sale", sale_id)
            delete_sale_assembly_plans_for_sale(sale_id, conn=conn)
            recalc_stock(conn=conn)
            lines_count = conn.execute("SELECT COUNT(*) FROM SalesLines WHERE sale_id=?", (sale_id,)).fetchone()[0]
            audit_event(
                "DOC_UNPOST",
                "Sale unposted",
                details={
                    "doc_date": doc_row["doc_date"],
                    "warehouse_id": doc_row["warehouse_id"],
                    "lines_count": int(lines_count),
                },
                related_doc_type="sale",
                related_doc_id=sale_id,
                conn=conn,
            )


# Inventory documents

def list_inventory_documents(
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    warehouse_id: Optional[int] = None,
) -> List[sqlite3.Row]:
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    query = (
        "SELECT i.id, i.doc_date, w.name as warehouse_name, i.status, "
        "COUNT(il.id) as lines_count, IFNULL(SUM(ABS(il.counted_qty - il.expected_qty)),0) as diff_total, "
        "i.comment "
        "FROM InventoryDocuments i "
        "LEFT JOIN Warehouses w ON w.id = i.warehouse_id "
        "LEFT JOIN InventoryLines il ON il.inventory_id = i.id"
    )
    clauses: List[str] = []
    params: List[object] = []
    if status:
        clauses.append("i.status=?")
        params.append(status)
    if date_from:
        clauses.append("i.doc_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("i.doc_date <= ?")
        params.append(date_to)
    if warehouse_id:
        clauses.append("i.warehouse_id = ?")
        params.append(warehouse_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " GROUP BY i.id ORDER BY i.doc_date, i.id"
    with get_connection() as conn:
        return list(conn.execute(query, params))


def get_latest_posted_stock_doc_date() -> Optional[str]:
    query = (
        "SELECT MAX(doc_date) as latest_date FROM ("
        "SELECT doc_date FROM PurchaseDocuments WHERE status='posted' "
        "UNION ALL "
        "SELECT doc_date FROM SalesDocuments WHERE status='posted' "
        "UNION ALL "
        "SELECT doc_date FROM InventoryDocuments WHERE status='posted'"
        ")"
    )
    with get_connection() as conn:
        row = conn.execute(query).fetchone()
    return row["latest_date"] if row and row["latest_date"] else None


def get_inventory_document(doc_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM InventoryDocuments WHERE id=?", (doc_id,)).fetchone()


def list_inventory_lines(doc_id: int) -> List[sqlite3.Row]:
    with get_connection() as conn:
        return list(
            conn.execute(
                "SELECT il.product_id, p.sku, p.name, il.expected_qty, il.counted_qty, "
                "(il.counted_qty - il.expected_qty) as diff, il.cost_override, il.note "
                "FROM InventoryLines il "
                "JOIN Products p ON p.id = il.product_id "
                "WHERE il.inventory_id=? "
                "ORDER BY p.name",
                (doc_id,),
            )
        )


def create_inventory_document(doc_date: str, warehouse_id: int, comment: str = "") -> int:
    doc_date = _validate_iso_date(doc_date)
    with get_connection() as conn:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO InventoryDocuments (doc_date, warehouse_id, status, comment) VALUES (?, ?, 'draft', ?)",
                (doc_date, warehouse_id, comment.strip()),
            )
            return cur.lastrowid


def update_inventory_document(doc_id: int, doc_date: str, warehouse_id: int, comment: str) -> None:
    doc_date = _validate_iso_date(doc_date)
    with get_connection() as conn:
        with transaction(conn):
            status = conn.execute("SELECT status FROM InventoryDocuments WHERE id=?", (doc_id,)).fetchone()
            if not status:
                raise ValueError("Документ не знайдено")
            if status[0] != "draft":
                raise ValueError("Редагування можливе лише у чернетці")
            conn.execute(
                "UPDATE InventoryDocuments SET doc_date=?, warehouse_id=?, comment=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (doc_date, warehouse_id, comment.strip(), doc_id),
            )


def replace_inventory_lines(doc_id: int, lines: list[dict]) -> None:
    with get_connection() as conn:
        with transaction(conn):
            status = conn.execute("SELECT status FROM InventoryDocuments WHERE id=?", (doc_id,)).fetchone()
            if not status:
                raise ValueError("Документ не знайдено")
            if status[0] != "draft":
                raise ValueError("Рядки можна змінювати лише у чернетці")
            conn.execute("DELETE FROM InventoryLines WHERE inventory_id=?", (doc_id,))
            for line in lines:
                product_id = int(line["product_id"])
                expected_qty = float(line.get("expected_qty", 0) or 0)
                counted_qty = float(line.get("counted_qty", 0) or 0)
                cost_override = line.get("cost_override")
                cost_value = None if cost_override in (None, "") else float(cost_override)
                note = (line.get("note") or "").strip()
                conn.execute(
                    "INSERT INTO InventoryLines (inventory_id, product_id, expected_qty, counted_qty, cost_override, note) "
                    "VALUES (?,?,?,?,?,?)",
                    (doc_id, product_id, expected_qty, counted_qty, cost_value, note),
                )


def delete_inventory_document(doc_id: int) -> None:
    with get_connection() as conn:
        with transaction(conn):
            status = conn.execute("SELECT status FROM InventoryDocuments WHERE id=?", (doc_id,)).fetchone()
            if not status:
                raise ValueError("Документ не знайдено")
            if status[0] != "draft":
                raise ValueError("Видаляти можна лише чернетки")
            conn.execute("DELETE FROM InventoryDocuments WHERE id=?", (doc_id,))


def _inventory_in_cost(
    conn: sqlite3.Connection,
    product_id: int,
    warehouse_id: int,
    doc_date: str,
    current_qty: float,
    avg_cost: float,
    cost_override: Optional[float],
) -> float:
    eps = 1e-9
    if cost_override is not None and cost_override > eps:
        return float(cost_override)
    if current_qty > eps and avg_cost > eps:
        return float(avg_cost)
    last_price = _get_last_purchase_price(conn, product_id, warehouse_id, doc_date)
    if last_price > eps:
        return float(last_price)
    return 0.0


def post_inventory(doc_id: int) -> None:
    with get_connection() as conn:
        with transaction(conn):
            doc = conn.execute(
                "SELECT status, doc_date, warehouse_id FROM InventoryDocuments WHERE id=?",
                (doc_id,),
            ).fetchone()
            if not doc:
                raise ValueError("Документ не знайдено")
            if doc["status"] != "draft":
                raise ValueError("Документ вже проведено")
            lines = conn.execute(
                "SELECT product_id, counted_qty, cost_override FROM InventoryLines WHERE inventory_id=?",
                (doc_id,),
            ).fetchall()
            if not lines:
                raise ValueError("Немає рядків для проведення")
            for line in lines:
                counted_qty = float(line["counted_qty"] or 0.0)
                cost_override = line["cost_override"]
                if counted_qty < 0:
                    raise ValueError("Фактична кількість не може бути від'ємною")
                if cost_override is not None and float(cost_override) < 0:
                    raise ValueError("Собівартість не може бути від'ємною")
                current_qty, avg_cost = _get_balance(conn, line["product_id"], doc["warehouse_id"])
                if counted_qty - current_qty > 1e-9:
                    cost = _inventory_in_cost(
                        conn,
                        line["product_id"],
                        doc["warehouse_id"],
                        doc["doc_date"],
                        current_qty,
                        avg_cost,
                        cost_override,
                    )
                    if cost <= 0:
                        raise ValueError("Для надлишку потрібна собівартість")
            conn.execute("UPDATE InventoryDocuments SET status='posted', updated_at=CURRENT_TIMESTAMP WHERE id=?", (doc_id,))
            recalc_stock(conn=conn)
            audit_event(
                "DOC_POST",
                "Inventory posted",
                details={
                    "doc_date": doc["doc_date"],
                    "warehouse_id": doc["warehouse_id"],
                    "lines_count": len(lines),
                },
                related_doc_type="inventory",
                related_doc_id=doc_id,
                conn=conn,
            )


def unpost_inventory(doc_id: int) -> None:
    with get_connection() as conn:
        with transaction(conn):
            doc = conn.execute(
                "SELECT status, doc_date, warehouse_id FROM InventoryDocuments WHERE id=?", (doc_id,)
            ).fetchone()
            if not doc:
                raise ValueError("Документ не знайдено")
            if doc["status"] != "posted":
                raise ValueError("Документ не проведено")
            conn.execute("UPDATE InventoryDocuments SET status='draft', updated_at=CURRENT_TIMESTAMP WHERE id=?", (doc_id,))
            recalc_stock(conn=conn)
            lines_count = conn.execute(
                "SELECT COUNT(*) FROM InventoryLines WHERE inventory_id=?", (doc_id,)
            ).fetchone()[0]
            audit_event(
                "DOC_UNPOST",
                "Inventory unposted",
                details={
                    "doc_date": doc["doc_date"],
                    "warehouse_id": doc["warehouse_id"],
                    "lines_count": int(lines_count),
                },
                related_doc_type="inventory",
                related_doc_id=doc_id,
                conn=conn,
            )


# Stock listing

def list_stock(search: Optional[str] = None) -> List[sqlite3.Row]:
    query = (
        "SELECT p.id as product_id, p.name, p.sku, w.name as warehouse, sb.warehouse_id, IFNULL(sb.quantity,0) as quantity, IFNULL(sb.average_cost,0) as average_cost "
        "FROM Products p "
        "JOIN Warehouses w ON 1=1 "
        "LEFT JOIN StockBalances sb ON sb.product_id = p.id AND sb.warehouse_id = w.id"
    )
    query += " ORDER BY p.name, w.name"
    with get_connection() as conn:
        rows = list(conn.execute(query))
    if not search:
        return rows
    needle = norm_text(search)
    filtered: List[sqlite3.Row] = []
    for row in rows:
        haystack = norm_text(f"{row['name']} {row['sku']} {row['warehouse'] or ''}")
        if needle in haystack:
            filtered.append(row)
    return filtered


def list_stock_moves(
    product_id: int,
    warehouse_id: int | None = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[sqlite3.Row]:
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    query = (
        "SELECT sm.id, sm.move_date, w.name AS warehouse_name, sm.warehouse_id, sm.qty_in, sm.qty_out, sm.cost_per_unit, "
        "sm.amount, sm.reference_type, sm.reference_id, sm.channel, cp.name AS counterparty_name "
        "FROM StockMoves sm "
        "LEFT JOIN Warehouses w ON w.id = sm.warehouse_id "
        "LEFT JOIN Counterparties cp ON cp.id = sm.counterparty_id "
        "WHERE sm.product_id=?"
    )
    params: list[object] = [product_id]
    if warehouse_id:
        query += " AND sm.warehouse_id=?"
        params.append(warehouse_id)
    if date_from:
        query += " AND sm.move_date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND sm.move_date <= ?"
        params.append(date_to)
    query += " ORDER BY sm.move_date DESC, sm.id DESC"
    with get_connection() as conn:
        return list(conn.execute(query, tuple(params)))


# Cash transactions

def add_cash_transaction(
    date: str,
    amount: float,
    ctype: str,
    counterparty_id: Optional[int],
    related_doc_type: Optional[str],
    related_doc_id: Optional[int],
    channel: str = "",
    comment: str = "",
    currency_code: str = "UAH",
    exchange_rate: float = 1.0,
    amount_doc: float | None = None,
) -> int:
    date = _normalize_date(date, field_label="Дата")
    amount_doc = amount if amount_doc is None else amount_doc
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO CashTransactions (date, amount, type, counterparty_id, related_doc_type, related_doc_id, channel, comment, currency_code, exchange_rate, amount_doc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                date,
                amount,
                ctype,
                counterparty_id,
                related_doc_type,
                related_doc_id,
                channel.strip(),
                comment.strip(),
                currency_code.strip() or "UAH",
                float(exchange_rate or 1.0),
                amount_doc,
            ),
        )
        conn.commit()
        return cur.lastrowid


def list_cash(date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[sqlite3.Row]:
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    query = (
        "SELECT ct.id, ct.date, ct.amount, ct.amount_doc, ct.currency_code, ct.exchange_rate, ct.type, ct.counterparty_id, cp.name as counterparty, ct.related_doc_type, ct.related_doc_id, ct.channel, ct.comment "
        "FROM CashTransactions ct LEFT JOIN Counterparties cp ON cp.id = ct.counterparty_id"
    )
    clauses: List[str] = []
    params: List[object] = []
    if date_from:
        clauses.append("ct.date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("ct.date <= ?")
        params.append(date_to)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY ct.date, ct.id"
    with get_connection() as conn:
        return list(conn.execute(query, params))


def list_audit_events(limit: int = 500, *, event_type: str | None = None, text: str | None = None) -> List[dict]:
    with get_connection() as conn:
        if not _audit_table_exists(conn):
            return []
        params = {"limit": int(limit), "event_type": event_type, "text": text}
        rows = conn.execute(
            """
            SELECT id, created_at, event_type, level, message, details_json, related_doc_type, related_doc_id, actor, host, pid
            FROM AuditLog
            WHERE (:event_type IS NULL OR event_type = :event_type)
              AND (:text IS NULL OR message LIKE '%' || :text || '%')
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def purge_audit_log(keep_last: int = 20000) -> int:
    with get_connection() as conn:
        if not _audit_table_exists(conn):
            return 0
        with safe_transaction(conn):
            cur = conn.execute(
                "DELETE FROM AuditLog WHERE id NOT IN (SELECT id FROM AuditLog ORDER BY id DESC LIMIT ?)",
                (int(keep_last),),
            )
            return int(cur.rowcount or 0)


# Reporting helpers

def _sale_income_by_product(date_from: Optional[str], date_to: Optional[str]) -> Dict[int, float]:
    """Distribute sale payments across products proportionally to line amounts."""
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    result: Dict[int, float] = {}
    with get_connection() as conn:
        payments = conn.execute(
            "SELECT id, amount, related_doc_id FROM CashTransactions WHERE type='sale_payment'"
            + (" AND date >= ?" if date_from else "")
            + (" AND date <= ?" if date_to else ""),
            tuple(x for x in [date_from, date_to] if x),
        ).fetchall()
        for pay in payments:
            sale_id = pay["related_doc_id"]
            if not sale_id:
                continue
            lines = conn.execute("SELECT product_id, amount FROM SalesLines WHERE sale_id=?", (sale_id,)).fetchall()
            total = sum(float(l["amount"]) for l in lines)
            if not total:
                continue
            for ln in lines:
                share = float(ln["amount"]) / total
                result[ln["product_id"]] = result.get(ln["product_id"], 0.0) + pay["amount"] * share
    return result


def _sale_expenses_by_product(date_from: Optional[str], date_to: Optional[str]) -> Dict[int, float]:
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    clauses = ["s.status='posted'"]
    params: List[object] = []
    if date_from:
        clauses.append("s.doc_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("s.doc_date <= ?")
        params.append(date_to)
    where = " WHERE " + " AND ".join(clauses)
    query = (
        "SELECT sl.product_id, SUM(sl.order_expense_allocated_base) as expense "
        "FROM SalesLines sl JOIN SalesDocuments s ON s.id = sl.sale_id" + where + " GROUP BY sl.product_id"
    )
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return {row["product_id"]: float(row["expense"] or 0.0) for row in rows}


def profit_by_product(date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[dict]:
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    income_map = _sale_income_by_product(date_from, date_to)
    expense_map = _sale_expenses_by_product(date_from, date_to)
    assembly_rows: list[sqlite3.Row] = []
    with get_connection() as conn:
        params: List[object] = []
        query = "SELECT product_id, SUM(amount) as cogs FROM StockMoves WHERE reference_type='sale'"
        if date_from:
            query += " AND move_date >= ?"
            params.append(date_from)
        if date_to:
            query += " AND move_date <= ?"
            params.append(date_to)
        query += " GROUP BY product_id"
        cogs_rows = conn.execute(query, params).fetchall()
        extra_params: List[object] = []
        extra_query = (
            "SELECT ecca.product_id, SUM(ecca.amount_cogs_base) as cogs "
            "FROM ExtraCostCogsAllocations ecca "
            "JOIN ExtraCostDocuments e ON e.id = ecca.extra_cost_id "
            "WHERE e.status='posted'"
        )
        if date_from:
            extra_query += " AND e.doc_date >= ?"
            extra_params.append(date_from)
        if date_to:
            extra_query += " AND e.doc_date <= ?"
            extra_params.append(date_to)
        extra_query += " GROUP BY ecca.product_id"
        extra_cogs_rows = conn.execute(extra_query, extra_params).fetchall()
        product_names = {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM Products")}
        assembly_params: List[object] = []
        assembly_query = (
            "SELECT sap.parent_product_id as product_id, SUM(ABS(sapl.amount)) as cogs "
            "FROM SaleAssemblyPlanLines sapl "
            "JOIN SaleAssemblyPlans sap ON sap.id = sapl.plan_id "
            "JOIN SalesDocuments s ON s.id = sap.sale_id "
            "WHERE s.status='posted'"
        )
        if date_from:
            assembly_query += " AND s.doc_date >= ?"
            assembly_params.append(date_from)
        if date_to:
            assembly_query += " AND s.doc_date <= ?"
            assembly_params.append(date_to)
        assembly_query += " GROUP BY sap.parent_product_id"
        assembly_rows = conn.execute(assembly_query, assembly_params).fetchall()
    cogs_map = {row["product_id"]: abs(float(row["cogs"])) for row in cogs_rows}
    for row in assembly_rows:
        pid = row["product_id"]
        cogs_map[pid] = cogs_map.get(pid, 0.0) + float(row["cogs"] or 0.0)
    for row in extra_cogs_rows:
        pid = row["product_id"]
        cogs_map[pid] = cogs_map.get(pid, 0.0) + float(row["cogs"] or 0.0)
    results: List[dict] = []
    for pid in sorted(set(cogs_map.keys()) | set(income_map.keys())):
        income = income_map.get(pid, 0.0)
        expenses = expense_map.get(pid, 0.0)
        cogs = cogs_map.get(pid, 0.0)
        results.append(
            {
                "product_id": pid,
                "product": product_names.get(pid, ""),
                "income": income,
                "cogs": cogs,
                "expenses": expenses,
                "gross_profit": income - cogs - expenses,
            }
        )
    return sorted(results, key=lambda r: r["product"])


def cash_flow_summary(date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[dict]:
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    clauses: List[str] = []
    params: List[object] = []
    if date_from:
        clauses.append("date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("date <= ?")
        params.append(date_to)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    query = "SELECT type, SUM(amount) as total FROM CashTransactions" + where + " GROUP BY type"
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [{"type": row["type"], "total": float(row["total"])} for row in rows]


def dashboard_trends(date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[dict]:
    """Monthly turnover and gross profit for trend charts."""
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")

    def _date_clause(field: str) -> tuple[str, List[object]]:
        clauses: List[str] = []
        params: List[object] = []
        if date_from:
            clauses.append(f"{field} >= ?")
            params.append(date_from)
        if date_to:
            clauses.append(f"{field} <= ?")
            params.append(date_to)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    sale_clause, sale_params = _date_clause("s.doc_date")
    cash_clause, cash_params = _date_clause("date")
    move_clause, move_params = _date_clause("s.doc_date")
    extra_clause, extra_params = _date_clause("e.doc_date")
    if extra_clause:
        extra_clause = extra_clause.replace(" WHERE ", " AND ", 1)

    with get_connection() as conn:
        turnover_rows = conn.execute(
            "SELECT strftime('%Y-%m', date) as period, IFNULL(SUM(amount),0) as total "
            "FROM CashTransactions "
            "WHERE type='sale_payment'" + cash_clause + " GROUP BY strftime('%Y-%m', date) ORDER BY period",
            cash_params,
        ).fetchall()
        revenue_rows = conn.execute(
            "SELECT strftime('%Y-%m', s.doc_date) as period, IFNULL(SUM(sl.amount),0) as revenue, "
            "IFNULL(SUM(sl.order_expense_allocated_base),0) as expenses "
            "FROM SalesLines sl JOIN SalesDocuments s ON s.id = sl.sale_id "
            "WHERE s.status='posted'" + sale_clause + " GROUP BY strftime('%Y-%m', s.doc_date) ORDER BY period",
            sale_params,
        ).fetchall()
        cogs_rows = conn.execute(
            "SELECT strftime('%Y-%m', s.doc_date) as period, IFNULL(SUM(sm.amount),0) as cogs "
            "FROM StockMoves sm JOIN SalesDocuments s ON s.id = sm.reference_id "
            "WHERE sm.reference_type='sale'" + move_clause + " GROUP BY strftime('%Y-%m', s.doc_date) ORDER BY period",
            move_params,
        ).fetchall()
        extra_cogs_rows = conn.execute(
            "SELECT strftime('%Y-%m', e.doc_date) as period, IFNULL(SUM(ecca.amount_cogs_base),0) as cogs "
            "FROM ExtraCostCogsAllocations ecca "
            "JOIN ExtraCostDocuments e ON e.id = ecca.extra_cost_id "
            "WHERE e.status='posted'" + extra_clause + " GROUP BY strftime('%Y-%m', e.doc_date) ORDER BY period",
            extra_params,
        ).fetchall()

    revenue_map = {row["period"]: float(row["revenue"]) for row in revenue_rows}
    expense_map = {row["period"]: float(row["expenses"]) for row in revenue_rows}
    cogs_map = {row["period"]: -float(row["cogs"]) for row in cogs_rows}
    extra_cogs_map = {row["period"]: float(row["cogs"]) for row in extra_cogs_rows}
    periods = sorted(
        {row["period"] for row in turnover_rows} | set(revenue_map.keys()) | set(cogs_map.keys()) | set(extra_cogs_map.keys())
    )
    results: List[dict] = []
    for period in periods:
        turnover_val = next((float(r["total"]) for r in turnover_rows if r["period"] == period), 0.0)
        gross_profit_val = (
            revenue_map.get(period, 0.0)
            - cogs_map.get(period, 0.0)
            - extra_cogs_map.get(period, 0.0)
            - expense_map.get(period, 0.0)
        )
        results.append({"period": period, "turnover": turnover_val, "gross_profit": gross_profit_val})
    return results


def dashboard_metrics(date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    """Key metrics: turnover, gross profit, margin, stock value."""
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    clauses: List[str] = []
    params: List[object] = []
    if date_from:
        clauses.append("date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("date <= ?")
        params.append(date_to)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with get_connection() as conn:
        turnover = conn.execute(
            "SELECT IFNULL(SUM(amount),0) FROM CashTransactions WHERE type='sale_payment'" + where,
            params,
        ).fetchone()[0]
        gross_profit = sum(r["gross_profit"] for r in profit_by_product(date_from, date_to))
        stock_value = conn.execute(
            "SELECT IFNULL(SUM(quantity * average_cost),0) FROM StockBalances"
        ).fetchone()[0]
    margin_pct = (gross_profit / turnover * 100) if turnover else 0.0
    return {
        "turnover": float(turnover),
        "gross_profit": float(gross_profit),
        "margin_pct": float(margin_pct),
        "stock_value": float(stock_value),
    }


def sales_analysis(
    date_from: Optional[str] = None, date_to: Optional[str] = None
) -> dict:
    """Sales by channels and categories with revenue and gross profit."""
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")

    def _date_clause(prefix: str) -> Tuple[str, List[object]]:
        clauses: List[str] = []
        params: List[object] = []
        if date_from:
            clauses.append(f"{prefix} >= ?")
            params.append(date_from)
        if date_to:
            clauses.append(f"{prefix} <= ?")
            params.append(date_to)
        sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        return sql, params

    with get_connection() as conn:
        sale_clause, sale_params = _date_clause("s.doc_date")
        move_clause, move_params = _date_clause("move_date")
        extra_clause, extra_params = _date_clause("e.doc_date")
        if extra_clause:
            extra_clause = extra_clause.replace(" WHERE ", " AND ", 1)

        channel_rows = conn.execute(
            "SELECT COALESCE(s.channel,'') as channel, IFNULL(SUM(sl.amount),0) as revenue, "
            "IFNULL(SUM(sl.quantity),0) as qty "
            "FROM SalesLines sl JOIN SalesDocuments s ON s.id = sl.sale_id "
            "WHERE s.status='posted'" + sale_clause + " GROUP BY COALESCE(s.channel,'') ORDER BY revenue DESC",
            sale_params,
        ).fetchall()
        channel_cogs = conn.execute(
            "SELECT COALESCE(channel,'') as channel, IFNULL(SUM(amount),0) as cogs "
            "FROM StockMoves WHERE reference_type='sale'" + move_clause + " GROUP BY COALESCE(channel,'')",
            move_params,
        ).fetchall()
        cogs_map = {row["channel"]: -float(row["cogs"]) for row in channel_cogs}
        extra_cogs_total = conn.execute(
            "SELECT IFNULL(SUM(ecca.amount_cogs_base),0) as cogs "
            "FROM ExtraCostCogsAllocations ecca "
            "JOIN ExtraCostDocuments e ON e.id = ecca.extra_cost_id "
            "WHERE e.status='posted'" + extra_clause,
            extra_params,
        ).fetchone()["cogs"]
        cogs_map[""] = cogs_map.get("", 0.0) + float(extra_cogs_total or 0.0)

        channel_expenses = conn.execute(
            "SELECT COALESCE(s.channel,'') as channel, IFNULL(SUM(sl.order_expense_allocated_base),0) as expenses "
            "FROM SalesLines sl JOIN SalesDocuments s ON s.id = sl.sale_id "
            "WHERE s.status='posted'" + sale_clause + " GROUP BY COALESCE(s.channel,'')",
            sale_params,
        ).fetchall()
        expense_map = {row["channel"]: float(row["expenses"]) for row in channel_expenses}

        category_rows = conn.execute(
            "SELECT COALESCE(c.name,'Без категорії') as category, IFNULL(SUM(sl.amount),0) as revenue, "
            "IFNULL(SUM(sl.quantity),0) as qty "
            "FROM SalesLines sl "
            "JOIN SalesDocuments s ON s.id = sl.sale_id "
            "JOIN Products p ON p.id = sl.product_id "
            "LEFT JOIN Categories c ON c.id = p.category_id "
            "WHERE s.status='posted'" + sale_clause + " GROUP BY COALESCE(c.name,'Без категорії') ORDER BY revenue DESC",
            sale_params,
        ).fetchall()
        category_cogs_rows = conn.execute(
            "SELECT p.category_id, IFNULL(SUM(sm.amount),0) as cogs "
            "FROM StockMoves sm "
            "JOIN SalesDocuments s ON s.id = sm.reference_id "
            "JOIN Products p ON p.id = sm.product_id "
            "WHERE sm.reference_type='sale'" + move_clause + " GROUP BY p.category_id",
            move_params,
        ).fetchall()
        extra_category_cogs = conn.execute(
            "SELECT p.category_id, IFNULL(SUM(ecca.amount_cogs_base),0) as cogs "
            "FROM ExtraCostCogsAllocations ecca "
            "JOIN ExtraCostDocuments e ON e.id = ecca.extra_cost_id "
            "JOIN Products p ON p.id = ecca.product_id "
            "WHERE e.status='posted'" + extra_clause + " GROUP BY p.category_id",
            extra_params,
        ).fetchall()
        category_expenses_rows = conn.execute(
            "SELECT p.category_id, IFNULL(SUM(sl.order_expense_allocated_base),0) as expenses "
            "FROM SalesLines sl "
            "JOIN SalesDocuments s ON s.id = sl.sale_id "
            "JOIN Products p ON p.id = sl.product_id "
            "WHERE s.status='posted'" + sale_clause + " GROUP BY p.category_id",
            sale_params,
        ).fetchall()
        category_names = {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM Categories")}
        cogs_by_category = {}
        for row in category_cogs_rows:
            name = category_names.get(row["category_id"], "Без категорії")
            cogs_by_category[name] = cogs_by_category.get(name, 0.0) - float(row["cogs"])
        for row in extra_category_cogs:
            name = category_names.get(row["category_id"], "Без категорії")
            cogs_by_category[name] = cogs_by_category.get(name, 0.0) + float(row["cogs"])
        expenses_by_category: Dict[str, float] = {}
        for row in category_expenses_rows:
            name = category_names.get(row["category_id"], "Без категорії")
            expenses_by_category[name] = expenses_by_category.get(name, 0.0) + float(row["expenses"])

    channels = [
        {
            "name": row["channel"] or "Без каналу",
            "revenue": float(row["revenue"]),
            "gross_profit": float(row["revenue"]) - cogs_map.get(row["channel"], 0.0) - expense_map.get(row["channel"], 0.0),
            "qty": float(row["qty"]),
        }
        for row in channel_rows
    ]

    categories = [
        {
            "name": row["category"],
            "revenue": float(row["revenue"]),
            "gross_profit": float(row["revenue"]) - cogs_by_category.get(row["category"], 0.0) - expenses_by_category.get(row["category"], 0.0),
            "qty": float(row["qty"]),
        }
        for row in category_rows
    ]
    return {"channels": channels, "categories": categories}


def abc_xyz_report(date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[dict]:
    """ABC by revenue and XYZ by demand variability (monthly quantities)."""
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    data: Dict[int, dict] = {}
    clauses: List[str] = ["s.status='posted'"]
    params: List[object] = []
    if date_from:
        clauses.append("s.doc_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("s.doc_date <= ?")
        params.append(date_to)
    where = " WHERE " + " AND ".join(clauses)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT p.id as product_id, p.name, p.sku, c.name as category, s.doc_date, sl.quantity, sl.amount "
            "FROM SalesLines sl "
            "JOIN SalesDocuments s ON s.id = sl.sale_id "
            "JOIN Products p ON p.id = sl.product_id "
            "LEFT JOIN Categories c ON c.id = p.category_id" + where,
            params,
        ).fetchall()

    for row in rows:
        pid = row["product_id"]
        info = data.setdefault(
            pid,
            {
                "product_id": pid,
                "name": row["name"],
                "sku": row["sku"],
                "category": row["category"] or "Без категорії",
                "revenue": 0.0,
                "monthly_qty": {},
            },
        )
        info["revenue"] += float(row["amount"])
        month = (row["doc_date"] or "")[:7]
        info["monthly_qty"][month] = info["monthly_qty"].get(month, 0.0) + float(row["quantity"])

    total_revenue = sum(item["revenue"] for item in data.values()) or 1.0
    sorted_products = sorted(data.values(), key=lambda x: x["revenue"], reverse=True)
    cumulative = 0.0
    results: List[dict] = []
    for item in sorted_products:
        cumulative += item["revenue"]
        share = cumulative / total_revenue
        if share <= 0.8:
            abc = "A"
        elif share <= 0.95:
            abc = "B"
        else:
            abc = "C"

        qty_values = list(item["monthly_qty"].values())
        if not qty_values or math.isclose(sum(qty_values), 0.0):
            xyz = "Z"
        elif len(qty_values) == 1:
            xyz = "X"
        else:
            qty_mean = mean(qty_values)
            cv = (pstdev(qty_values) / qty_mean) if qty_mean else float("inf")
            if cv <= 0.1:
                xyz = "X"
            elif cv <= 0.25:
                xyz = "Y"
            else:
                xyz = "Z"
        results.append(
            {
                "product_id": item["product_id"],
                "name": item["name"],
                "sku": item["sku"],
                "category": item["category"],
                "revenue": item["revenue"],
                "abc": abc,
                "xyz": xyz,
            }
        )
    return results


def cash_flow_detailed(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    ctype: Optional[str] = None,
    channel: Optional[str] = None,
    counterparty_id: Optional[int] = None,
) -> List[dict]:
    date_from = _normalize_optional_date(date_from, field_label="Дата з")
    date_to = _normalize_optional_date(date_to, field_label="Дата по")
    clauses: List[str] = []
    params: List[object] = []
    if date_from:
        clauses.append("ct.date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("ct.date <= ?")
        params.append(date_to)
    if ctype:
        clauses.append("ct.type = ?")
        params.append(ctype)
    if channel:
        clauses.append("ct.channel LIKE ?")
        params.append(f"%{channel}%")
    if counterparty_id:
        clauses.append("ct.counterparty_id = ?")
        params.append(counterparty_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    query = (
        "SELECT ct.id, ct.date, ct.amount, ct.type, ct.channel, ct.comment, "
        "cp.name as counterparty FROM CashTransactions ct "
        "LEFT JOIN Counterparties cp ON cp.id = ct.counterparty_id" + where + " ORDER BY ct.date, ct.id"
    )
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row["id"],
            "date": row["date"],
            "amount": float(row["amount"]),
            "type": row["type"],
            "channel": row["channel"] or "",
            "comment": row["comment"] or "",
            "counterparty": row["counterparty"] or "",
        }
        for row in rows
    ]


def export_table_to_csv(table: str, output_path: Path) -> None:
    import csv

    with get_connection() as conn, output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        rows = conn.execute(f"SELECT * FROM {table}")
        writer.writerow([col[0] for col in rows.description])
        writer.writerows(rows)
    logging.info("Exported %s to %s", table, output_path)


# Label template helpers
def list_label_templates(active_only: bool = True) -> list[sqlite3.Row]:
    query = "SELECT * FROM LabelTemplates"
    params: list = []
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY is_default DESC, title"
    with get_connection() as conn:
        return conn.execute(query, params).fetchall()


def get_label_template(template_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM LabelTemplates WHERE id = ?", (template_id,)).fetchone()


def get_label_template_by_code(code: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM LabelTemplates WHERE code = ?", (code,)).fetchone()


def list_label_template_elements(template_id: int, active_only: bool = True) -> list[sqlite3.Row]:
    query = "SELECT * FROM LabelTemplateElements WHERE template_id = ?"
    params: list = [template_id]
    if active_only:
        query += " AND is_active = 1"
    query += " ORDER BY sort_order, id"
    with get_connection() as conn:
        return conn.execute(query, params).fetchall()


def get_label_template_full(template_id: int) -> dict | None:
    tpl_row = get_label_template(template_id)
    if not tpl_row:
        return None
    elements = list_label_template_elements(template_id, active_only=False)
    parsed = []
    for el in elements:
        options: dict
        try:
            options = json.loads(el["options_json"] or "{}")
        except Exception:
            options = {}
        parsed.append({**dict(el), "options": options})
    return {"template": dict(tpl_row), "elements": parsed}


def _validate_template_payload(payload: dict) -> None:
    tpl = payload.get("template") or {}
    required_numeric_positive = [
        "page_w_mm",
        "page_h_mm",
        "cols",
        "rows",
        "label_w_mm",
        "label_h_mm",
    ]
    for key in required_numeric_positive:
        if tpl.get(key) is None or float(tpl[key]) <= 0:
            raise ValueError(f"Некоректне поле шаблону: {key}")
    if tpl.get("code"):
        tpl["code"] = str(tpl["code"]).strip()
    if not tpl.get("code"):
        raise ValueError("Код шаблону обов'язковий")
    if tpl.get("kind") not in ("sheet", "thermal"):
        raise ValueError("Непідтримуваний тип шаблону")


def create_label_template(payload: dict) -> int:
    _validate_template_payload(payload)
    tpl = payload.get("template") or {}
    elements = payload.get("elements") or []
    with get_connection() as conn:
        with safe_transaction(conn):
            cur = conn.execute(
                """
                INSERT INTO LabelTemplates (
                    code, title, kind, page_w_mm, page_h_mm, orientation, cols, rows, label_w_mm, label_h_mm,
                    gap_x_mm, gap_y_mm, margin_left_mm, margin_top_mm, margin_right_mm, margin_bottom_mm,
                    offset_x_mm, offset_y_mm, scale_x, scale_y, is_active, is_default
                ) VALUES (
                    :code, :title, :kind, :page_w_mm, :page_h_mm, :orientation, :cols, :rows, :label_w_mm, :label_h_mm,
                    :gap_x_mm, :gap_y_mm, :margin_left_mm, :margin_top_mm, :margin_right_mm, :margin_bottom_mm,
                    :offset_x_mm, :offset_y_mm, :scale_x, :scale_y, :is_active, :is_default
                )
                """,
                tpl,
            )
            tpl_id = cur.lastrowid
            for order, element in enumerate(elements):
                options_json = json.dumps(element.get("options", {}))
                conn.execute(
                    """
                    INSERT INTO LabelTemplateElements (
                        template_id, element_type, field_key, x_mm, y_mm, w_mm, h_mm, rotation_deg, align, font_name,
                        font_size, max_chars, wrap, options_json, sort_order, is_active
                    ) VALUES (
                        :template_id, :element_type, :field_key, :x_mm, :y_mm, :w_mm, :h_mm, :rotation_deg, :align, :font_name,
                        :font_size, :max_chars, :wrap, :options_json, :sort_order, :is_active
                    )
                    """,
                    {
                        **element,
                        "template_id": tpl_id,
                        "sort_order": element.get("sort_order", order),
                        "options_json": options_json,
                    },
                )
            if int(tpl.get("is_default", 0)):
                conn.execute("UPDATE LabelTemplates SET is_default = 0 WHERE id <> ?", (tpl_id,))
                conn.execute("UPDATE LabelTemplates SET is_default = 1 WHERE id = ?", (tpl_id,))
            return tpl_id


def update_label_template(template_id: int, payload: dict) -> None:
    _validate_template_payload(payload)
    tpl = payload.get("template") or {}
    elements = payload.get("elements") or []
    with get_connection() as conn:
        with safe_transaction(conn):
            conn.execute(
                """
                UPDATE LabelTemplates SET
                    code=:code, title=:title, kind=:kind, page_w_mm=:page_w_mm, page_h_mm=:page_h_mm,
                    orientation=:orientation, cols=:cols, rows=:rows, label_w_mm=:label_w_mm, label_h_mm=:label_h_mm,
                    gap_x_mm=:gap_x_mm, gap_y_mm=:gap_y_mm, margin_left_mm=:margin_left_mm, margin_top_mm=:margin_top_mm,
                    margin_right_mm=:margin_right_mm, margin_bottom_mm=:margin_bottom_mm, offset_x_mm=:offset_x_mm,
                    offset_y_mm=:offset_y_mm, scale_x=:scale_x, scale_y=:scale_y, is_active=:is_active,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id = :id
                """,
                {**tpl, "id": template_id},
            )
            conn.execute("DELETE FROM LabelTemplateElements WHERE template_id = ?", (template_id,))
            for order, element in enumerate(elements):
                options_json = json.dumps(element.get("options", {}))
                conn.execute(
                    """
                    INSERT INTO LabelTemplateElements (
                        template_id, element_type, field_key, x_mm, y_mm, w_mm, h_mm, rotation_deg, align, font_name,
                        font_size, max_chars, wrap, options_json, sort_order, is_active
                    ) VALUES (
                        :template_id, :element_type, :field_key, :x_mm, :y_mm, :w_mm, :h_mm, :rotation_deg, :align, :font_name,
                        :font_size, :max_chars, :wrap, :options_json, :sort_order, :is_active
                    )
                    """,
                    {
                        **element,
                        "template_id": template_id,
                        "sort_order": element.get("sort_order", order),
                        "options_json": options_json,
                    },
                )
            if int(tpl.get("is_default", 0)):
                conn.execute("UPDATE LabelTemplates SET is_default = 0 WHERE id <> ?", (template_id,))
                conn.execute("UPDATE LabelTemplates SET is_default = 1 WHERE id = ?", (template_id,))


def delete_label_template(template_id: int) -> None:
    tpl = get_label_template(template_id)
    if not tpl:
        return
    if tpl["is_default"]:
        raise ValueError("Спочатку призначте інший шаблон типовим")
    with get_connection() as conn:
        conn.execute("DELETE FROM LabelTemplates WHERE id = ?", (template_id,))


def set_default_label_template(template_id: int) -> None:
    with get_connection() as conn:
        with safe_transaction(conn):
            conn.execute("UPDATE LabelTemplates SET is_default = 0")
            conn.execute("UPDATE LabelTemplates SET is_default = 1 WHERE id = ?", (template_id,))


def duplicate_label_template(template_id: int, new_code: str, new_title: str) -> int:
    data = get_label_template_full(template_id)
    if not data:
        raise ValueError("Шаблон не знайдено")
    tpl = data["template"].copy()
    tpl.update({"code": new_code, "title": new_title, "is_default": 0})
    return create_label_template({"template": tpl, "elements": data["elements"]})
