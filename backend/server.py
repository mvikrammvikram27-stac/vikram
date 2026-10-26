import hashlib
import json
import mimetypes
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import mysql.connector
from mysql.connector import IntegrityError


BACKEND_DIR = Path(__file__).resolve().parent
SITE_DIR = BACKEND_DIR.parent
MAX_BODY_SIZE = 1024 * 1024
SUBMISSION_TABLES = {
    "reservations": "vcafe_reservations",
    "feedback": "vcafe_feedback",
    "contacts": "vcafe_contacts",
    "newsletters": "vcafe_newsletters",
}
RESERVATION_COLUMNS = {
    "guest_name": "VARCHAR(50) NULL",
    "guest_email": "VARCHAR(320) NULL",
    "guest_phone": "VARCHAR(20) NULL",
    "res_pin": "VARCHAR(4) NULL",
    "res_date": "DATE NULL",
    "res_time": "TIME NULL",
    "guest_count": "INT UNSIGNED NULL",
    "event_slot": "DATETIME NULL",
    "dining_package": "VARCHAR(50) NULL",
    "seating_area": "VARCHAR(30) NULL",
    "special_req": "VARCHAR(255) NULL",
    "ref_code": "VARCHAR(100) NULL",
    "special_notes": "TEXT NULL",
    "bill_amount": "DECIMAL(10, 2) NULL",
    "terms_agree": "BOOLEAN NOT NULL DEFAULT FALSE",
}


def load_environment():
    env_path = BACKEND_DIR / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_environment()

PORT = int(os.environ.get("PORT", "5000"))
DATABASE = os.environ.get("MYSQL_DATABASE", "vcafe_db")
if not re.fullmatch(r"[A-Za-z0-9_]+", DATABASE):
    raise ValueError("MYSQL_DATABASE may contain only letters, numbers, and underscores.")

MYSQL_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "charset": "utf8mb4",
}


def connect(database=None):
    config = dict(MYSQL_CONFIG)
    if database:
        config["database"] = database
    return mysql.connector.connect(**config)


def initialize_database():
    connection = connect()
    try:
        cursor = connection.cursor()
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{DATABASE}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cursor.close()
    finally:
        connection.close()

    connection = connect(DATABASE)
    try:
        cursor = connection.cursor()
        statements = [
            """
            CREATE TABLE IF NOT EXISTS vcafe_reservations (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
              guest_name VARCHAR(50) NULL,
              guest_email VARCHAR(320) NULL,
              guest_phone VARCHAR(20) NULL,
              res_pin VARCHAR(4) NULL,
              res_date DATE NULL,
              res_time TIME NULL,
              guest_count INT UNSIGNED NULL,
              event_slot DATETIME NULL,
              dining_package VARCHAR(50) NULL,
              seating_area VARCHAR(30) NULL,
              special_req VARCHAR(255) NULL,
              ref_code VARCHAR(100) NULL,
              special_notes TEXT NULL,
              bill_amount DECIMAL(10, 2) NULL,
              terms_agree BOOLEAN NOT NULL DEFAULT FALSE,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB
            """,
            """
            CREATE TABLE IF NOT EXISTS vcafe_feedback (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
              payload JSON NOT NULL,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB
            """,
            """
            CREATE TABLE IF NOT EXISTS vcafe_contacts (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
              payload JSON NOT NULL,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB
            """,
            """
            CREATE TABLE IF NOT EXISTS vcafe_newsletters (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
              email VARCHAR(320) NOT NULL UNIQUE,
              payload JSON NOT NULL,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB
            """,
            """
            CREATE TABLE IF NOT EXISTS vcafe_users (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
              user_id VARCHAR(32) NOT NULL UNIQUE,
              name VARCHAR(100) NOT NULL,
              email VARCHAR(320) NOT NULL UNIQUE,
              phone VARCHAR(20) NOT NULL,
              dob DATE NULL,
              gender VARCHAR(30) NULL,
              password_hash VARCHAR(255) NOT NULL,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB
            """,
        ]
        for statement in statements:
            cursor.execute(statement)
        migrate_reservations(cursor, connection)
        cursor.close()
    finally:
        connection.close()


def reservation_values(payload):
    special_req = payload.get("special_req") or []
    if isinstance(special_req, str):
        special_req = [special_req] if special_req else []
    if not isinstance(special_req, list):
        special_req = [str(special_req)]
    special_req = [str(item) for item in special_req if item is not None]

    guest_count = payload.get("guest_count")
    if guest_count in (None, ""):
        guest_count = None
    else:
        guest_count = int(guest_count)
        if guest_count < 0:
            raise ValueError("guest_count cannot be negative")

    bill_amount = payload.get("bill_amount")
    if bill_amount in (None, ""):
        bill_amount = None
    else:
        amount_text = re.sub(r"[₹,\s]", "", str(bill_amount))
        try:
            bill_amount = Decimal(amount_text)
        except InvalidOperation as error:
            raise ValueError("bill_amount must be a valid amount") from error
        if not bill_amount.is_finite() or bill_amount < 0:
            raise ValueError("bill_amount must be a non-negative amount")

    return (
        payload.get("guest_name") or None,
        payload.get("guest_email") or None,
        payload.get("guest_phone") or None,
        payload.get("res_pin") or None,
        payload.get("res_date") or None,
        payload.get("res_time") or None,
        guest_count,
        payload.get("event_slot") or None,
        payload.get("dining_package") or None,
        payload.get("seating_area") or None,
        ",".join(special_req) or None,
        payload.get("ref_code") or None,
        payload.get("special_notes") or None,
        bill_amount,
        bool(payload.get("terms_agree")),
    )


def migrate_reservations(cursor, connection):
    cursor.execute("SHOW COLUMNS FROM vcafe_reservations")
    existing_columns = {row[0] for row in cursor.fetchall()}
    for column, definition in RESERVATION_COLUMNS.items():
        if column not in existing_columns:
            cursor.execute(f"ALTER TABLE vcafe_reservations ADD COLUMN `{column}` {definition}")

    if "payload" not in existing_columns:
        return

    cursor.execute("SELECT id, payload FROM vcafe_reservations ORDER BY id")
    legacy_rows = cursor.fetchall()
    update_sql = """
        UPDATE vcafe_reservations
        SET guest_name = %s, guest_email = %s, guest_phone = %s, res_pin = %s,
            res_date = %s, res_time = %s, guest_count = %s, event_slot = %s,
            dining_package = %s, seating_area = %s, special_req = %s,
            ref_code = %s, special_notes = %s, bill_amount = %s, terms_agree = %s
        WHERE id = %s
    """
    for row_id, raw_payload in legacy_rows:
        payload = decode_payload(raw_payload)
        if not isinstance(payload, dict):
            raise ValueError(f"Reservation {row_id} contains invalid legacy data.")
        cursor.execute(update_sql, (*reservation_values(payload), row_id))
    connection.commit()
    cursor.execute("ALTER TABLE vcafe_reservations DROP COLUMN payload")


def format_reservation(row):
    special_req = row["special_req"] or ""
    if isinstance(special_req, bytes):
        special_req = special_req.decode("utf-8")
    bill_amount = row["bill_amount"]
    if bill_amount is not None:
        amount = Decimal(str(bill_amount))
        bill_amount = f"₹{amount:,.0f}" if amount % 1 == 0 else f"₹{amount:,.2f}"

    res_date = row["res_date"]
    res_time = row["res_time"]
    event_slot = row["event_slot"]
    return {
        "id": row["id"],
        "guest_name": row["guest_name"],
        "guest_email": row["guest_email"],
        "guest_phone": row["guest_phone"],
        "res_pin": row["res_pin"] or "",
        "res_date": res_date.isoformat() if res_date else "",
        "res_time": res_time.strftime("%H:%M") if res_time else "",
        "guest_count": str(row["guest_count"]) if row["guest_count"] is not None else "",
        "event_slot": event_slot.strftime("%Y-%m-%dT%H:%M") if event_slot else "",
        "dining_package": row["dining_package"],
        "seating_area": row["seating_area"],
        "special_req": [item for item in special_req.split(",") if item] if special_req else [],
        "ref_code": row["ref_code"],
        "special_notes": row["special_notes"] or "",
        "bill_amount": bill_amount or "",
        "terms_agree": bool(row["terms_agree"]),
        "createdAt": iso_datetime(row["created_at"]),
    }


def iso_datetime(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def decode_payload(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value)


def hash_password(password):
    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64
    )
    return f"{salt.hex()}:{password_hash.hex()}"


class CafeHandler(BaseHTTPRequestHandler):
    server_version = "VCafePython/1.0"

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.handle_static(send_body=False)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            connection = None
            try:
                connection = connect(DATABASE)
                cursor = connection.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.close()
                self.send_json(200, {
                    "status": "ok",
                    "message": f"The V-Cafe backend is connected to {DATABASE}",
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                })
            except mysql.connector.Error as error:
                print(f"Database health check failed: {error}", file=sys.stderr)
                self.send_database_error()
            finally:
                if connection is not None and connection.is_connected():
                    connection.close()
            return

        if parsed.path == "/api/submissions":
            self.get_submissions()
            return

        self.handle_static()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self.read_payload()
        except ValueError as error:
            self.send_json(400, {"success": False, "message": str(error)})
            return

        if parsed.path == "/api/register":
            self.register(payload)
            return

        endpoint_map = {
            "/api/reservation": ("reservations", "Reservation submitted successfully."),
            "/api/feedback": ("feedback", "Feedback submitted successfully."),
            "/api/contact": ("contacts", "Contact inquiry saved successfully."),
            "/api/newsletter": ("newsletters", "Newsletter subscription confirmed."),
        }
        endpoint = endpoint_map.get(parsed.path)
        if endpoint:
            self.save_submission(payload, *endpoint)
            return

        self.send_json(404, {"success": False, "message": "Endpoint not found"})

    def read_payload(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid Content-Length header") from error
        if content_length < 0 or content_length > MAX_BODY_SIZE:
            raise ValueError("Request body is too large")

        raw_body = self.rfile.read(content_length) if content_length else b""
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if not raw_body:
            return {}
        if content_type == "application/x-www-form-urlencoded":
            from urllib.parse import parse_qs

            return {key: values[-1] for key, values in parse_qs(raw_body.decode("utf-8")).items()}
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid JSON body") from error
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def get_submissions(self):
        connection = None
        try:
            connection = connect(DATABASE)
            cursor = connection.cursor(dictionary=True)
            data = {}
            for key, table in SUBMISSION_TABLES.items():
                if key == "reservations":
                    cursor.execute(
                        "SELECT * FROM vcafe_reservations ORDER BY id DESC"
                    )
                    data[key] = [format_reservation(row) for row in cursor.fetchall()]
                else:
                    cursor.execute(
                        f"SELECT id, payload, created_at FROM `{table}` ORDER BY id DESC"
                    )
                    rows = cursor.fetchall()
                    data[key] = [
                        {
                            **decode_payload(row["payload"]),
                            "id": row["id"],
                            "createdAt": iso_datetime(row["created_at"]),
                        }
                        for row in rows
                    ]

            cursor.execute(
                """
                SELECT id, user_id AS userId, name, email, phone, gender, created_at
                FROM vcafe_users ORDER BY id DESC
                """
            )
            data["users"] = [
                {
                    **{key: value for key, value in row.items() if key != "created_at"},
                    "createdAt": iso_datetime(row["created_at"]),
                }
                for row in cursor.fetchall()
            ]
            cursor.close()
            self.send_json(200, {"success": True, "data": data})
        except (mysql.connector.Error, ValueError, TypeError) as error:
            print(f"Database request failed: {error}", file=sys.stderr)
            self.send_database_error()
        finally:
            if connection is not None and connection.is_connected():
                connection.close()

    def register(self, payload):
        name = payload.get("name")
        email = payload.get("email")
        phone = payload.get("phone")
        password = payload.get("password")
        if (
            not isinstance(name, str)
            or len(name.strip()) < 3
            or not isinstance(email, str)
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
            or not isinstance(phone, str)
            or not re.fullmatch(r"[0-9]{10}", phone)
            or not isinstance(password, str)
            or len(password) < 6
        ):
            self.send_json(400, {
                "success": False,
                "message": "Please provide valid registration details.",
            })
            return

        user_id = f"USR-{int(time.time() * 1000)}-{secrets.token_hex(3).upper()}"
        connection = None
        try:
            connection = connect(DATABASE)
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO vcafe_users
                    (user_id, name, email, phone, dob, gender, password_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    name.strip(),
                    email.strip().lower(),
                    phone,
                    payload.get("dob") or None,
                    payload.get("gender") or None,
                    hash_password(password),
                ),
            )
            connection.commit()
            cursor.close()
            self.send_json(201, {
                "success": True,
                "message": "Registration successful! Welcome to The V-Cafe.",
                "userId": user_id,
            })
        except IntegrityError as error:
            if connection is not None:
                connection.rollback()
            if error.errno == 1062:
                self.send_json(409, {
                    "success": False,
                    "message": "Email already registered. Please login.",
                })
            else:
                print(f"Database request failed: {error}", file=sys.stderr)
                self.send_database_error()
        except mysql.connector.Error as error:
            if connection is not None:
                connection.rollback()
            print(f"Database request failed: {error}", file=sys.stderr)
            self.send_database_error()
        finally:
            if connection is not None and connection.is_connected():
                connection.close()

    def save_submission(self, payload, table, message):
        connection = None
        record = {**payload, "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        try:
            connection = connect(DATABASE)
            cursor = connection.cursor()
            if table == "reservations":
                values = reservation_values(payload)
                cursor.execute(
                    """
                    INSERT INTO vcafe_reservations (
                        guest_name, guest_email, guest_phone, res_pin, res_date,
                        res_time, guest_count, event_slot, dining_package,
                        seating_area, special_req, ref_code, special_notes,
                        bill_amount, terms_agree
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    values,
                )
            elif table == "newsletters":
                email = payload.get("email")
                if not isinstance(email, str) or not re.fullmatch(
                    r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip()
                ):
                    self.send_json(400, {
                        "success": False,
                        "message": "Please provide a valid email address.",
                    })
                    return
                record["email"] = email.strip().lower()
                cursor.execute(
                    "INSERT INTO vcafe_newsletters (email, payload) VALUES (%s, %s)",
                    (record["email"], json.dumps(record, ensure_ascii=False)),
                )
            else:
                cursor.execute(
                    f"INSERT INTO `{SUBMISSION_TABLES[table]}` (payload) VALUES (%s)",
                    (json.dumps(record, ensure_ascii=False),),
                )
            record_id = cursor.lastrowid
            connection.commit()
            cursor.close()
            self.send_json(201, {
                "success": True,
                "message": message,
                "data": {**record, "id": record_id},
            })
        except IntegrityError as error:
            if connection is not None:
                connection.rollback()
            if table == "newsletters" and error.errno == 1062:
                self.send_json(200, {"success": True, "message": "Already subscribed!"})
            else:
                print(f"Database request failed: {error}", file=sys.stderr)
                self.send_database_error()
        except ValueError as error:
            if connection is not None:
                connection.rollback()
            self.send_json(400, {"success": False, "message": str(error)})
        except mysql.connector.Error as error:
            if connection is not None:
                connection.rollback()
            print(f"Database request failed: {error}", file=sys.stderr)
            self.send_database_error()
        finally:
            if connection is not None and connection.is_connected():
                connection.close()

    def send_database_error(self):
        self.send_json(500, {
            "success": False,
            "message": "The database request failed. Check the backend logs.",
        })

    def handle_static(self, send_body=True):
        parsed_path = unquote(urlparse(self.path).path)
        if parsed_path == "/backend" or parsed_path.startswith("/backend/"):
            self.send_error(404)
            return

        relative_path = parsed_path.lstrip("/") or "index.html"
        file_path = (SITE_DIR / relative_path).resolve()
        if not file_path.is_relative_to(SITE_DIR) or not file_path.is_file():
            self.send_error(404)
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        content = file_path.read_bytes()
        self.send_response(200)
        content_type = content_type or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if send_body:
            self.wfile.write(content)


class CafeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    try:
        initialize_database()
        server = CafeServer(("0.0.0.0", PORT), CafeHandler)
    except (mysql.connector.Error, OSError, ValueError) as error:
        print(f'Unable to start V-Cafe backend: {error}', file=sys.stderr)
        print("Check MySQL is running and backend/.env contains valid connection settings.", file=sys.stderr)
        return 1

    print(f"The V-Cafe Python backend is running on http://localhost:{PORT}", flush=True)
    print(f'Connected to MySQL database "{DATABASE}".', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping the V-Cafe backend.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
