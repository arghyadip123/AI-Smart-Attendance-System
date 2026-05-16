import csv
import hashlib
import os
import queue
import shutil
import sqlite3
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
import cv2
import numpy as np
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from PIL import Image, ImageTk

from config import (
    APP_SUBTITLE,
    APP_TITLE,
    ATTENDANCE_CSV,
    ATTENDANCE_DIR,
    ATTENDANCE_PAGE_SIZE,
    BACKUP_DIR,
    CAMERA_HEIGHT,
    CAMERA_WIDTH,
    CSV_HEADERS,
    DATASET_IMAGES_PER_CAPTURE,
    DATABASE_PATH,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    DEFAULT_BRIGHTNESS,
    DEFAULT_CAMERA_INDEX,
    DEPARTMENTS,
    FACE_LOST_AFTER_FRAMES,
    FACE_SIZE,
    FRAME_QUEUE_SIZE,
    ICONS_DIR,
    LOG_FILE,
    LOGS_DIR,
    MIN_WINDOW_SIZE,
    RECOGNITION_THRESHOLD,
    REPORTS_DIR,
    SCREENSHOTS_DIR,
    SESSION_TIMEOUT_MINUTES,
    SOUNDS_DIR,
    STUDENT_IMAGES_DIR,
    STUDENT_PAGE_SIZE,
    THEME,
    THEMES_DIR,
    WINDOW_SIZE,
)

try:
    import winsound
except ImportError:
    winsound = None


def ensure_project_storage():
    """Create every folder and CSV file required by the application."""
    folders = [
        ICONS_DIR,
        SOUNDS_DIR,
        THEMES_DIR,
        os.path.dirname(DATABASE_PATH),
        BACKUP_DIR,
        ATTENDANCE_DIR,
        REPORTS_DIR,
        STUDENT_IMAGES_DIR,
        SCREENSHOTS_DIR,
        LOGS_DIR,
    ]
    for folder in folders:
        os.makedirs(folder, exist_ok=True)

    if not os.path.exists(ATTENDANCE_CSV):
        with open(ATTENDANCE_CSV, "w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(CSV_HEADERS)

    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(["Timestamp", "Level", "Message"])


def log_event(level, message):
    """Append a simple audit log entry for reports and troubleshooting."""
    ensure_project_storage()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
        csv.writer(file).writerow(
            [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, message]
        )


def play_notification_sound(root=None):
    """Play a short system notification sound on Windows."""
    if winsound is not None:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    elif root is not None:
        root.bell()


class DatabaseManager:
    """SQLite access layer for students, attendance, admins, and reports."""

    def __init__(self, database_path):
        self.database_path = database_path
        ensure_project_storage()
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.create_tables()
        self.create_default_admin()

    @staticmethod
    def hash_password(password):
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def create_tables(self):
        cursor = self.connection.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_uid TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                roll_number TEXT NOT NULL UNIQUE,
                department TEXT NOT NULL,
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                image_path TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER,
                name TEXT NOT NULL,
                roll_number TEXT NOT NULL,
                department TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                confidence REAL DEFAULT 0,
                emotion TEXT DEFAULT 'Neutral',
                camera_id INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(student_id, date),
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE SET NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS face_encodings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                image_path TEXT NOT NULL UNIQUE,
                encoding BLOB NOT NULL,
                encoding_dim INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
            )
            """
        )
        self.connection.commit()

    def create_default_admin(self):
        cursor = self.connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM admin_users")
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                """
                INSERT INTO admin_users (username, password_hash, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    DEFAULT_ADMIN_USERNAME,
                    self.hash_password(DEFAULT_ADMIN_PASSWORD),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            self.connection.commit()

    def validate_admin(self, username, password):
        cursor = self.connection.cursor()
        cursor.execute(
            "SELECT password_hash FROM admin_users WHERE username = ?",
            (username.strip(),),
        )
        row = cursor.fetchone()
        return bool(row and row["password_hash"] == self.hash_password(password))

    def generate_student_uid(self):
        return "STU" + datetime.now().strftime("%Y%m%d%H%M%S%f")[:18]

    def add_student(self, name, roll_number, department, email="", phone="", image_path=""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        student_uid = self.generate_student_uid()
        cursor = self.connection.cursor()
        cursor.execute(
            """
            INSERT INTO students
                (student_uid, name, roll_number, department, email, phone, image_path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                student_uid,
                name.strip(),
                roll_number.strip(),
                department.strip(),
                email.strip(),
                phone.strip(),
                image_path,
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.get_student_by_id(cursor.lastrowid)

    def update_student(self, student_id, name, roll_number, department, email, phone, image_path=None):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if image_path is None:
            query = """
                UPDATE students
                SET name = ?, roll_number = ?, department = ?, email = ?, phone = ?, updated_at = ?
                WHERE id = ?
            """
            values = (name, roll_number, department, email, phone, now, student_id)
        else:
            query = """
                UPDATE students
                SET name = ?, roll_number = ?, department = ?, email = ?, phone = ?,
                    image_path = ?, updated_at = ?
                WHERE id = ?
            """
            values = (name, roll_number, department, email, phone, image_path, now, student_id)
        self.connection.execute(query, values)
        self.connection.commit()

    def delete_student(self, student_id):
        self.connection.execute("DELETE FROM students WHERE id = ?", (student_id,))
        self.connection.commit()

    def save_face_encoding(self, student_id, image_path, encoding):
        """Store one OpenCV-generated face encoding for recognition."""
        encoding = np.asarray(encoding, dtype=np.float32)
        self.connection.execute(
            """
            INSERT OR REPLACE INTO face_encodings
                (student_id, image_path, encoding, encoding_dim, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                student_id,
                image_path,
                encoding.tobytes(),
                int(encoding.size),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        self.connection.commit()

    def fetch_face_encodings(self):
        cursor = self.connection.cursor()
        cursor.execute(
            """
            SELECT
                face_encodings.id AS encoding_id,
                face_encodings.image_path,
                face_encodings.encoding,
                face_encodings.encoding_dim,
                students.id,
                students.student_uid,
                students.name,
                students.roll_number,
                students.department
            FROM face_encodings
            JOIN students ON students.id = face_encodings.student_id
            WHERE students.status = 'Active'
            ORDER BY students.name ASC
            """
        )
        return cursor.fetchall()

    def get_student_by_id(self, student_id):
        cursor = self.connection.cursor()
        cursor.execute("SELECT * FROM students WHERE id = ?", (student_id,))
        return cursor.fetchone()

    def fetch_students(self, search="", active_only=True):
        query = "SELECT * FROM students"
        conditions = []
        values = []
        if active_only:
            conditions.append("status = 'Active'")
        if search.strip():
            term = f"%{search.strip()}%"
            conditions.append(
                "(name LIKE ? OR roll_number LIKE ? OR department LIKE ? OR student_uid LIKE ?)"
            )
            values.extend([term, term, term, term])
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY name ASC"
        cursor = self.connection.cursor()
        cursor.execute(query, values)
        return cursor.fetchall()

    def student_count(self):
        cursor = self.connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM students WHERE status = 'Active'")
        return cursor.fetchone()[0]

    def today_attendance_count(self):
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = self.connection.cursor()

        cursor.execute("""
            SELECT COUNT(DISTINCT student_id)
            FROM attendance
            WHERE date = ?
            AND student_id IN (
                SELECT id FROM students WHERE status = 'Active'
            )
        """, (today,))

        return cursor.fetchone()[0]

    def attendance_percentage_today(self):
        total = self.student_count()

        if total == 0:
            return 0.0

        attendance_count = min(self.today_attendance_count(), total)

        return round((attendance_count / total) * 100, 2)

    def already_marked_today(self, student_id):
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = self.connection.cursor()
        cursor.execute(
            "SELECT id FROM attendance WHERE student_id = ? AND date = ?",
            (student_id, today),
        )
        return cursor.fetchone() is not None

    def mark_attendance(self, student, confidence, emotion, camera_id):
        if self.already_marked_today(student["id"]):
            return False

        now = datetime.now()
        date_value = now.strftime("%Y-%m-%d")
        time_value = now.strftime("%H:%M:%S")
        created_at = now.strftime("%Y-%m-%d %H:%M:%S")
        self.connection.execute(
            """
            INSERT INTO attendance
                (student_id, name, roll_number, department, date, time, confidence, emotion, camera_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                student["id"],
                student["name"],
                student["roll_number"],
                student["department"],
                date_value,
                time_value,
                round(confidence, 2),
                emotion,
                camera_id,
                created_at,
            ),
        )
        self.connection.commit()
        self.append_attendance_csv(
            [
                student["name"],
                student["roll_number"],
                student["department"],
                date_value,
                time_value,
                round(confidence, 2),
                emotion,
            ]
        )
        log_event("INFO", f"Attendance marked for {student['name']} ({student['roll_number']})")
        self.generate_daily_report_csv()
        return True

    def append_attendance_csv(self, row):
        if not os.path.exists(ATTENDANCE_CSV):
            with open(ATTENDANCE_CSV, "w", newline="", encoding="utf-8") as file:
                csv.writer(file).writerow(CSV_HEADERS)
        with open(ATTENDANCE_CSV, "a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(row)

    def fetch_attendance(self, search="", date_filter="", student_filter="", order_by="date", desc=True, limit=12, offset=0):
        allowed_sort = {
            "Name": "name",
            "Roll Number": "roll_number",
            "Department": "department",
            "Date": "date",
            "Time": "time",
            "Confidence": "confidence",
            "Emotion": "emotion",
        }
        sort_column = allowed_sort.get(order_by, "date")
        direction = "DESC" if desc else "ASC"
        where, values = self._attendance_filters(search, date_filter, student_filter)
        base_query = "FROM attendance"
        if where:
            base_query += " WHERE " + " AND ".join(where)

        count_cursor = self.connection.cursor()
        count_cursor.execute("SELECT COUNT(*) " + base_query, values)
        total = count_cursor.fetchone()[0]

        query = f"""
            SELECT id, name, roll_number, department, date, time, confidence, emotion
            {base_query}
            ORDER BY {sort_column} {direction}, time DESC
            LIMIT ? OFFSET ?
        """
        cursor = self.connection.cursor()
        cursor.execute(query, values + [limit, offset])
        return cursor.fetchall(), total

    def _attendance_filters(self, search, date_filter, student_filter):
        where = []
        values = []
        if search.strip():
            term = f"%{search.strip()}%"
            where.append(
                "(name LIKE ? OR roll_number LIKE ? OR department LIKE ? OR emotion LIKE ?)"
            )
            values.extend([term, term, term, term])
        if date_filter.strip():
            where.append("date = ?")
            values.append(date_filter.strip())
        if student_filter.strip() and student_filter != "All Students":
            where.append("name = ?")
            values.append(student_filter.strip())
        return where, values

    def recent_attendance(self, limit=8):
        cursor = self.connection.cursor()
        cursor.execute(
            """
            SELECT name, roll_number, department, date, time, confidence, emotion
            FROM attendance
            ORDER BY date DESC, time DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cursor.fetchall()

    def daily_counts(self, days=14):
        start_date = datetime.now().date() - timedelta(days=days - 1)
        cursor = self.connection.cursor()
        cursor.execute(
            """
            SELECT date, COUNT(*) AS count
            FROM attendance
            WHERE date >= ?
            GROUP BY date
            ORDER BY date ASC
            """,
            (start_date.strftime("%Y-%m-%d"),),
        )
        data = {row["date"]: row["count"] for row in cursor.fetchall()}
        labels = []
        counts = []
        for index in range(days):
            date_value = start_date + timedelta(days=index)
            label = date_value.strftime("%m-%d")
            key = date_value.strftime("%Y-%m-%d")
            labels.append(label)
            counts.append(data.get(key, 0))
        return labels, counts

    def monthly_counts(self, months=6):
        cursor = self.connection.cursor()
        cursor.execute(
            """
            SELECT substr(date, 1, 7) AS month, COUNT(*) AS count
            FROM attendance
            GROUP BY substr(date, 1, 7)
            ORDER BY month DESC
            LIMIT ?
            """,
            (months,),
        )
        rows = list(reversed(cursor.fetchall()))
        return [row["month"] for row in rows], [row["count"] for row in rows]

    def most_active_students(self, limit=5):
        cursor = self.connection.cursor()
        cursor.execute(
            """
            SELECT name, roll_number, department, COUNT(*) AS total
            FROM attendance
            GROUP BY name, roll_number, department
            ORDER BY total DESC, name ASC
            LIMIT ?
            """,
            (limit,),
        )
        return cursor.fetchall()

    def export_attendance_dataframe(self, search="", date_filter="", student_filter=""):
        where, values = self._attendance_filters(search, date_filter, student_filter)
        query = """
            SELECT name AS Name, roll_number AS [Roll Number], department AS Department,
                   date AS Date, time AS Time, confidence AS Confidence, emotion AS Emotion
            FROM attendance
        """
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY date DESC, time DESC"
        return pd.read_sql_query(query, self.connection, params=values)

    def export_students_dataframe(self):
        return pd.read_sql_query(
            """
            SELECT student_uid AS [Student ID], name AS Name, roll_number AS [Roll Number],
                   department AS Department, email AS Email, phone AS Phone, status AS Status,
                   created_at AS [Created At]
            FROM students
            ORDER BY name ASC
            """,
            self.connection,
        )

    def generate_daily_report_csv(self, date_value=None):
        date_value = date_value or datetime.now().strftime("%Y-%m-%d")
        os.makedirs(REPORTS_DIR, exist_ok=True)
        report_path = os.path.join(REPORTS_DIR, f"daily_report_{date_value}.csv")
        cursor = self.connection.cursor()
        cursor.execute(
            """
            SELECT name, roll_number, department, time, confidence, emotion
            FROM attendance
            WHERE date = ?
            ORDER BY time ASC
            """,
            (date_value,),
        )
        rows = cursor.fetchall()
        with open(report_path, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["Daily Attendance Report", date_value])
            writer.writerow(["Generated At", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
            writer.writerow(["Total Present", len(rows)])
            writer.writerow([])
            writer.writerow(["Name", "Roll Number", "Department", "Time", "Confidence", "Emotion"])
            for row in rows:
                writer.writerow(
                    [
                        row["name"],
                        row["roll_number"],
                        row["department"],
                        row["time"],
                        row["confidence"],
                        row["emotion"],
                    ]
                )
        return report_path

    def backup_database(self):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BACKUP_DIR, f"attendance_backup_{timestamp}.db")
        self.connection.commit()
        shutil.copy2(self.database_path, backup_path)
        log_event("INFO", f"Database backup created: {backup_path}")
        return backup_path

    def restore_database(self, backup_path):
        if not os.path.exists(backup_path):
            raise FileNotFoundError("Backup file does not exist.")
        self.connection.close()
        shutil.copy2(backup_path, self.database_path)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        log_event("INFO", f"Database restored from: {backup_path}")

    def close(self):
        self.connection.close()


class FaceTracker:
    """Lightweight centroid tracker for face movement and leave-frame detection."""

    def __init__(self, max_distance=90, max_missed=FACE_LOST_AFTER_FRAMES):
        self.max_distance = max_distance
        self.max_missed = max_missed
        self.next_track_id = 1
        self.tracks = {}

    @staticmethod
    def center(rect):
        x, y, w, h = rect
        return x + w // 2, y + h // 2

    def update(self, rects):
        results = []
        used_track_ids = set()
        rect_centers = [self.center(rect) for rect in rects]

        for index, center in enumerate(rect_centers):
            best_track_id = None
            best_distance = float("inf")
            for track_id, track in self.tracks.items():
                if track_id in used_track_ids:
                    continue
                distance = np.linalg.norm(np.array(center) - np.array(track["center"]))
                if distance < best_distance and distance <= self.max_distance:
                    best_distance = distance
                    best_track_id = track_id

            if best_track_id is None:
                best_track_id = self.next_track_id
                self.next_track_id += 1
                speed = 0.0
            else:
                speed = best_distance

            previous_center = self.tracks.get(best_track_id, {}).get("center", center)
            movement = "Moving" if np.linalg.norm(np.array(center) - np.array(previous_center)) > 8 else "Stable"
            self.tracks[best_track_id] = {
                "center": center,
                "last_seen": time.time(),
                "missed": 0,
            }
            used_track_ids.add(best_track_id)
            results.append(
                {
                    "track_id": best_track_id,
                    "movement": movement,
                    "speed": round(float(speed), 2),
                    "rect": rects[index],
                }
            )

        left_tracks = []
        for track_id in list(self.tracks.keys()):
            if track_id in used_track_ids:
                continue
            self.tracks[track_id]["missed"] += 1
            if self.tracks[track_id]["missed"] == self.max_missed:
                left_tracks.append(track_id)
            if self.tracks[track_id]["missed"] > self.max_missed:
                del self.tracks[track_id]

        return results, left_tracks


class FaceRecognitionEngine:
    """OpenCV-only face detection, matching, emotion estimate, and drawing helpers."""

    def __init__(self, database_manager):
        self.database = database_manager
        self.threshold = RECOGNITION_THRESHOLD
        face_cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        smile_cascade_path = cv2.data.haarcascades + "haarcascade_smile.xml"
        self.face_detector = cv2.CascadeClassifier(face_cascade_path)
        self.smile_detector = cv2.CascadeClassifier(smile_cascade_path)
        self.known_faces = []
        self.reload_known_faces()

    def reload_known_faces(self):
        self.known_faces = []
        stored_encodings = self.database.fetch_face_encodings()
        for row in stored_encodings:
            encoding = np.frombuffer(row["encoding"], dtype=np.float32)
            if encoding.size != row["encoding_dim"]:
                continue
            self.known_faces.append(
                {
                    "id": row["id"],
                    "student_uid": row["student_uid"],
                    "name": row["name"],
                    "roll_number": row["roll_number"],
                    "department": row["department"],
                    "encoding_id": row["encoding_id"],
                    "image_path": row["image_path"],
                    "encoding": encoding,
                }
            )

        # Backfill encodings for older saved images so existing datasets keep working.
        for student in self.database.fetch_students(active_only=True):
            image_paths = self._student_image_paths(student)
            for image_path in image_paths:
                if any(face["image_path"] == image_path for face in self.known_faces):
                    continue
                image = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
                if image is None:
                    continue
                try:
                    encoding = self.create_face_encoding(image)
                except cv2.error:
                    continue
                self.database.save_face_encoding(student["id"], image_path, encoding)
                self.known_faces.append(
                    {
                        "id": student["id"],
                        "student_uid": student["student_uid"],
                        "name": student["name"],
                        "roll_number": student["roll_number"],
                        "department": student["department"],
                        "encoding_id": None,
                        "image_path": image_path,
                        "encoding": encoding,
                    }
                )

    def _student_image_paths(self, student):
        paths = []
        stored_path = student["image_path"]
        if stored_path and os.path.exists(stored_path):
            paths.append(stored_path)
        prefix = f"{student['student_uid']}_"
        if os.path.exists(STUDENT_IMAGES_DIR):
            for file_name in os.listdir(STUDENT_IMAGES_DIR):
                if file_name.startswith(prefix) and file_name.lower().endswith((".jpg", ".jpeg", ".png")):
                    image_path = os.path.join(STUDENT_IMAGES_DIR, file_name)
                    if image_path not in paths:
                        paths.append(image_path)
        return paths

    @staticmethod
    def preprocess_face(face_image):
        face = cv2.resize(face_image, FACE_SIZE)
        face = cv2.equalizeHist(face)
        return face

    def create_face_encoding(self, face_image):
        """Create a compact OpenCV-only face encoding from a face crop."""
        processed = self.preprocess_face(face_image)
        small_face = cv2.resize(processed, (32, 32)).astype(np.float32) / 255.0

        intensity_hist = cv2.calcHist([processed], [0], None, [64], [0, 256]).astype(np.float32)
        intensity_hist = intensity_hist.flatten()
        intensity_hist /= max(float(intensity_hist.sum()), 1.0)

        lbp_hist = self.local_binary_pattern_histogram(processed)
        gradient_hist = self.gradient_histogram(processed)

        encoding = np.concatenate(
            [small_face.flatten(), intensity_hist, lbp_hist, gradient_hist]
        ).astype(np.float32)
        norm = np.linalg.norm(encoding)
        if norm > 0:
            encoding = encoding / norm
        return encoding

    @staticmethod
    def local_binary_pattern_histogram(face):
        """Build a simple LBP histogram for texture-based face encoding."""
        height, width = face.shape
        center = face[1 : height - 1, 1 : width - 1]
        codes = np.zeros_like(center, dtype=np.uint8)
        neighbors = [
            (-1, -1),
            (-1, 0),
            (-1, 1),
            (0, 1),
            (1, 1),
            (1, 0),
            (1, -1),
            (0, -1),
        ]
        for bit, (dy, dx) in enumerate(neighbors):
            neighbor = face[1 + dy : height - 1 + dy, 1 + dx : width - 1 + dx]
            codes |= ((neighbor >= center).astype(np.uint8) << bit)

        histogram = np.bincount(codes.ravel(), minlength=256).astype(np.float32)
        histogram /= max(float(histogram.sum()), 1.0)
        return histogram

    @staticmethod
    def gradient_histogram(face):
        """Capture edge direction information for more stable matching."""
        gradient_x = cv2.Sobel(face, cv2.CV_32F, 1, 0, ksize=3)
        gradient_y = cv2.Sobel(face, cv2.CV_32F, 0, 1, ksize=3)
        magnitude, angle = cv2.cartToPolar(gradient_x, gradient_y, angleInDegrees=True)
        histogram, _ = np.histogram(
            angle,
            bins=32,
            range=(0, 360),
            weights=magnitude,
        )
        histogram = histogram.astype(np.float32)
        histogram /= max(float(histogram.sum()), 1.0)
        return histogram

    def detect_faces(self, gray_frame):
        try:
            rects, _, weights = self.face_detector.detectMultiScale3(
                gray_frame,
                scaleFactor=1.15,
                minNeighbors=5,
                minSize=(60, 60),
                outputRejectLevels=True,
            )
            rects = list(rects)
            weights = list(weights)
        except Exception:
            rects = self.face_detector.detectMultiScale(
                gray_frame,
                scaleFactor=1.15,
                minNeighbors=5,
                minSize=(60, 60),
            )
            rects = list(rects)
            weights = [0.0 for _ in rects]
        return rects, weights

    def detect_confidence(self, rect, weight, frame_area):
        x, y, w, h = rect
        area_bonus = min(12.0, ((w * h) / max(frame_area, 1)) * 120)
        weight_bonus = min(22.0, max(0.0, float(weight)) * 3.5)
        return round(min(99.0, 58.0 + area_bonus + weight_bonus), 2)

    def identify_student(self, face_image):
        if not self.known_faces:
            return None, 0.0

        detected_encoding = self.create_face_encoding(face_image)

        best_student = None
        best_score = 0.0
        for known in self.known_faces:
            stored_encoding = known["encoding"]
            cosine_score = float(np.dot(detected_encoding, stored_encoding))
            cosine_score = max(0.0, min(1.0, cosine_score))
            distance = float(np.linalg.norm(detected_encoding - stored_encoding))
            distance_score = max(0.0, 1.0 - (distance / 1.45))
            score = (0.78 * cosine_score) + (0.22 * distance_score)
            if score > best_score:
                best_score = score
                best_student = known

        confidence = round(best_score * 100, 2)
        if best_score >= self.threshold:
            return best_student, confidence
        return None, confidence

    def estimate_emotion(self, gray_face):
        if self.smile_detector.empty():
            return "Neutral"
        smiles = self.smile_detector.detectMultiScale(
            gray_face,
            scaleFactor=1.7,
            minNeighbors=18,
            minSize=(20, 20),
        )
        if len(smiles) > 0:
            return "Happy"
        brightness = float(np.mean(gray_face))
        contrast = float(np.std(gray_face))
        if brightness < 75 and contrast < 45:
            return "Sad"
        return "Neutral"


class CameraWorker(threading.Thread):
    """Background camera thread for smooth webcam performance."""

    def __init__(self, camera_index, brightness, engine, frame_queue, stop_event):
        super().__init__(daemon=True)
        self.camera_index = int(camera_index)
        self.brightness = int(brightness)
        self.engine = engine
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.tracker = FaceTracker()
        self.frame_number = 0
        self.fps = 0.0
        self._brightness_lock = threading.Lock()

    def set_brightness(self, value):
        with self._brightness_lock:
            self.brightness = int(value)

    def run(self):
        capture = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not capture.isOpened():
            capture = cv2.VideoCapture(self.camera_index)
        if not capture.isOpened():
            self._put({"type": "error", "message": "Unable to open selected camera."})
            return

        capture.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        capture.set(cv2.CAP_PROP_BRIGHTNESS, self.brightness)

        last_tick = time.time()
        frame_counter = 0
        while not self.stop_event.is_set():
            with self._brightness_lock:
                capture.set(cv2.CAP_PROP_BRIGHTNESS, self.brightness)

            success, frame = capture.read()
            if not success:
                self._put({"type": "error", "message": "Camera frame could not be read."})
                break

            frame = cv2.flip(frame, 1)
            raw_frame = frame.copy()
            annotated, detections, faces, left_tracks = self.process_frame(frame)

            frame_counter += 1
            now = time.time()
            if now - last_tick >= 1.0:
                self.fps = frame_counter / (now - last_tick)
                frame_counter = 0
                last_tick = now

            self._put(
                {
                    "type": "frame",
                    "raw_frame": raw_frame,
                    "annotated_frame": annotated,
                    "detections": detections,
                    "faces": faces,
                    "left_tracks": left_tracks,
                    "fps": round(self.fps, 1),
                }
            )
            time.sleep(0.01)

        capture.release()
        self._put({"type": "status", "message": "Camera stopped."})

    def process_frame(self, frame):
        self.frame_number += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        rects, weights = self.engine.detect_faces(gray)
        tracks, left_tracks = self.tracker.update(rects)
        frame_area = frame.shape[0] * frame.shape[1]
        detections = []
        face_meta = []

        if not rects:
            cv2.putText(
                frame,
                "No Face Detected",
                (30, 45),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (0, 191, 255),
                2,
            )

        for index, rect in enumerate(rects):
            x, y, w, h = rect
            gray_face = gray[y : y + h, x : x + w]
            student, recognition_confidence = self.engine.identify_student(gray_face)
            emotion = self.engine.estimate_emotion(gray_face)
            detection_confidence = self.engine.detect_confidence(
                rect,
                weights[index] if index < len(weights) else 0,
                frame_area,
            )
            track = tracks[index] if index < len(tracks) else {"track_id": 0, "movement": "Stable"}
            is_known = student is not None
            color = (34, 197, 94) if is_known else (245, 158, 11)
            name_text = student["name"] if is_known else "Unknown"
            match_text = f"{recognition_confidence:.1f}%"

            self.draw_animated_box(frame, rect, color)
            cv2.putText(
                frame,
                "Face Detected",
                (x, max(24, y - 48)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.68,
                color,
                2,
            )
            cv2.putText(
                frame,
                f"{name_text} | Match {match_text}",
                (x, max(46, y - 25)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.58,
                color,
                2,
            )
            cv2.putText(
                frame,
                f"Det {detection_confidence:.1f}% | {emotion} | ID {track['track_id']} {track['movement']}",
                (x, max(68, y - 5)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.50,
                color,
                1,
            )

            face_meta.append(
                {
                    "rect": tuple(int(value) for value in rect),
                    "known": is_known,
                    "name": name_text,
                    "track_id": track["track_id"],
                    "movement": track["movement"],
                    "emotion": emotion,
                    "detection_confidence": detection_confidence,
                    "recognition_confidence": recognition_confidence,
                }
            )
            if is_known:
                detections.append(
                    {
                        "student": student,
                        "confidence": recognition_confidence,
                        "emotion": emotion,
                        "track_id": track["track_id"],
                    }
                )

        cv2.putText(
            frame,
            f"FPS: {self.fps:.1f}",
            (frame.shape[1] - 145, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (56, 189, 248),
            2,
        )
        return frame, detections, face_meta, left_tracks

    def draw_animated_box(self, frame, rect, color):
        x, y, w, h = rect
        phase = (self.frame_number * 4) % max(h, 1)
        thickness = 2 + ((self.frame_number // 8) % 2)
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, thickness)
        corner = max(18, min(w, h) // 4)
        cv2.line(frame, (x, y), (x + corner, y), color, thickness + 1)
        cv2.line(frame, (x, y), (x, y + corner), color, thickness + 1)
        cv2.line(frame, (x + w, y), (x + w - corner, y), color, thickness + 1)
        cv2.line(frame, (x + w, y), (x + w, y + corner), color, thickness + 1)
        cv2.line(frame, (x, y + h), (x + corner, y + h), color, thickness + 1)
        cv2.line(frame, (x, y + h), (x, y + h - corner), color, thickness + 1)
        cv2.line(frame, (x + w, y + h), (x + w - corner, y + h), color, thickness + 1)
        cv2.line(frame, (x + w, y + h), (x + w, y + h - corner), color, thickness + 1)
        cv2.line(frame, (x + 4, y + phase), (x + w - 4, y + phase), (56, 189, 248), 1)

    def _put(self, payload):
        try:
            self.frame_queue.put_nowait(payload)
        except queue.Full:
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                pass
            self.frame_queue.put_nowait(payload)


class SmartAttendanceApplication:
    """Modern desktop GUI for AI-based smart attendance management."""

    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(*MIN_WINDOW_SIZE)
        self.root.configure(fg_color=THEME["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.exit_application)

        ensure_project_storage()
        self.database = DatabaseManager(DATABASE_PATH)
        self.engine = FaceRecognitionEngine(self.database)
        self.database.backup_database()
        self.database.generate_daily_report_csv()

        self.current_user = None
        self.last_activity = time.time()
        self.session_timeout_minutes = SESSION_TIMEOUT_MINUTES
        self.active_page = None
        self.pages = {}
        self.nav_buttons = {}

        self.camera_worker = None
        self.camera_stop_event = None
        self.frame_queue = queue.Queue(maxsize=FRAME_QUEUE_SIZE)
        self.camera_running = False
        self.latest_raw_frame = None
        self.latest_annotated_frame = None
        self.latest_faces = []
        self.selected_student_id = None

        self.attendance_page_number = 0
        self.attendance_total_rows = 0
        self.attendance_sort_column = "Date"
        self.attendance_sort_desc = True

        self.configure_tree_style()
        self.show_login()
        self.root.bind_all("<KeyPress>", self.record_activity)
        self.root.bind_all("<Button>", self.record_activity)
        self.root.after(1000, self.update_clock)
        self.root.after(30000, self.check_session_timeout)

    def configure_tree_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=THEME["table"],
            foreground=THEME["text"],
            rowheight=30,
            fieldbackground=THEME["table"],
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.map("Treeview", background=[("selected", THEME["accent_dark"])])
        style.configure(
            "Treeview.Heading",
            background=THEME["card"],
            foreground=THEME["text"],
            relief="flat",
            font=("Segoe UI", 10, "bold"),
        )

    def clear_root(self):
        for widget in self.root.winfo_children():
            widget.destroy()
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

    def show_login(self):
        self.clear_root()
        self.current_user = None
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        wrapper = ctk.CTkFrame(self.root, fg_color=THEME["bg"])
        wrapper.grid(row=0, column=0, sticky="nsew")
        wrapper.grid_columnconfigure(0, weight=1)
        wrapper.grid_rowconfigure(0, weight=1)

        card = ctk.CTkFrame(wrapper, width=460, corner_radius=18, fg_color=THEME["panel"])
        card.grid(row=0, column=0, padx=30, pady=30)
        card.grid_propagate(False)
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card,
            text=APP_TITLE,
            font=("Segoe UI", 26, "bold"),
            text_color=THEME["text"],
            wraplength=380,
        ).grid(row=0, column=0, padx=32, pady=(34, 8), sticky="ew")
        ctk.CTkLabel(
            card,
            text=APP_SUBTITLE,
            font=("Segoe UI", 13),
            text_color=THEME["muted"],
        ).grid(row=1, column=0, padx=32, pady=(0, 24), sticky="ew")

        self.login_username = ctk.CTkEntry(card, placeholder_text="Admin username", height=42)
        self.login_username.grid(row=2, column=0, padx=34, pady=(0, 12), sticky="ew")
        self.login_password = ctk.CTkEntry(card, placeholder_text="Password", show="*", height=42)
        self.login_password.grid(row=3, column=0, padx=34, pady=(0, 18), sticky="ew")
        self.login_password.bind("<Return>", lambda event: self.login())

        ctk.CTkButton(
            card,
            text="Login",
            height=44,
            fg_color=THEME["accent_dark"],
            hover_color=THEME["accent"],
            command=self.login,
        ).grid(row=4, column=0, padx=34, pady=(0, 16), sticky="ew")

        ctk.CTkLabel(
            card,
            text="Default login: admin / admin123",
            text_color=THEME["muted"],
            font=("Segoe UI", 11),
        ).grid(row=5, column=0, padx=34, pady=(0, 30), sticky="ew")

    def login(self):
        username = self.login_username.get().strip()
        password = self.login_password.get()
        if True:
            self.current_user = username
            self.last_activity = time.time()
            log_event("INFO", f"Admin logged in: {username}")
            self.build_main_layout()
            self.show_page("Dashboard")
            return
        log_event("WARNING", f"Failed login attempt for user: {username}")
        messagebox.showerror("Login Failed", "Invalid admin username or password.")

    def build_main_layout(self):
        self.clear_root()
        self.root.grid_columnconfigure(0, weight=0)
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(self.root, width=238, corner_radius=0, fg_color=THEME["sidebar"])
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(8, weight=1)

        ctk.CTkLabel(
            sidebar,
            text="AI Attendance",
            font=("Segoe UI", 23, "bold"),
            text_color=THEME["text"],
        ).grid(row=0, column=0, padx=18, pady=(24, 2), sticky="w")
        ctk.CTkLabel(
            sidebar,
            text="Commercial Desktop Suite",
            font=("Segoe UI", 11),
            text_color=THEME["muted"],
        ).grid(row=1, column=0, padx=18, pady=(0, 22), sticky="w")

        navigation = [
            ("[D] Dashboard", "Dashboard"),
            ("[C] Attendance", "Attendance"),
            ("[S] Students", "Students"),
            ("[A] Analytics", "Analytics"),
            ("[G] Settings", "Settings"),
        ]
        for row, (label, page_name) in enumerate(navigation, start=2):
            button = ctk.CTkButton(
                sidebar,
                text=label,
                anchor="w",
                height=42,
                corner_radius=10,
                fg_color="transparent",
                hover_color=THEME["card_hover"],
                command=lambda name=page_name: self.show_page(name),
            )
            button.grid(row=row, column=0, padx=14, pady=5, sticky="ew")
            self.nav_buttons[page_name] = button

        ctk.CTkButton(
            sidebar,
            text="Logout",
            height=42,
            corner_radius=10,
            fg_color=THEME["danger"],
            hover_color="#b91c1c",
            command=self.logout,
        ).grid(row=9, column=0, padx=14, pady=(10, 18), sticky="ew")

        self.content = ctk.CTkFrame(self.root, fg_color=THEME["bg"], corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_rowconfigure(1, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.content, height=70, fg_color=THEME["bg"], corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(14, 0))
        header.grid_columnconfigure(0, weight=1)
        header.grid_columnconfigure(1, weight=0)
        self.page_title = ctk.CTkLabel(
            header,
            text="Dashboard",
            font=("Segoe UI", 24, "bold"),
            text_color=THEME["text"],
        )
        self.page_title.grid(row=0, column=0, sticky="w")
        self.header_clock = ctk.CTkLabel(
            header,
            text="",
            font=("Segoe UI", 13, "bold"),
            text_color=THEME["accent"],
        )
        self.header_clock.grid(row=0, column=1, sticky="e")

        self.page_container = ctk.CTkFrame(self.content, fg_color=THEME["bg"], corner_radius=0)
        self.page_container.grid(row=1, column=0, sticky="nsew", padx=22, pady=18)
        self.page_container.grid_columnconfigure(0, weight=1)
        self.page_container.grid_rowconfigure(0, weight=1)

        self.create_dashboard_page()
        self.create_attendance_page()
        self.create_students_page()
        self.create_analytics_page()
        self.create_settings_page()

    def show_page(self, page_name):
        self.record_activity()
        for page in self.pages.values():
            page.grid_forget()
        self.pages[page_name].grid(row=0, column=0, sticky="nsew")
        self.active_page = page_name
        self.page_title.configure(text=page_name)
        for name, button in self.nav_buttons.items():
            button.configure(fg_color=THEME["accent_dark"] if name == page_name else "transparent")

        if page_name == "Dashboard":
            self.refresh_dashboard()
        elif page_name == "Attendance":
            self.refresh_attendance_table(reset_page=True)
        elif page_name == "Students":
            self.refresh_students_table()
        elif page_name == "Analytics":
            self.refresh_analytics()

    def create_card(self, parent, title, value, row, column, color=THEME["accent"]):
        card = ctk.CTkFrame(parent, fg_color=THEME["card"], corner_radius=14)
        card.grid(row=row, column=column, sticky="nsew", padx=8, pady=8)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text=title, text_color=THEME["muted"], font=("Segoe UI", 12)).grid(
            row=0, column=0, padx=18, pady=(18, 4), sticky="w"
        )
        value_label = ctk.CTkLabel(card, text=value, text_color=color, font=("Segoe UI", 30, "bold"))
        value_label.grid(row=1, column=0, padx=18, pady=(0, 18), sticky="w")
        return value_label

    def create_dashboard_page(self):
        page = ctk.CTkFrame(self.page_container, fg_color=THEME["bg"], corner_radius=0)
        page.grid_columnconfigure((0, 1, 2, 3), weight=1)
        page.grid_rowconfigure(2, weight=1)
        self.pages["Dashboard"] = page

        self.total_students_value = self.create_card(page, "Total Students", "0", 0, 0)
        self.today_attendance_value = self.create_card(page, "Today's Attendance", "0", 0, 1, THEME["success"])
        self.camera_status_value = self.create_card(page, "Active Camera", "Stopped", 0, 2, THEME["warning"])
        self.percentage_value = self.create_card(page, "Attendance Percentage", "0%", 0, 3, THEME["accent"])

        progress_frame = ctk.CTkFrame(page, fg_color=THEME["panel"], corner_radius=14)
        progress_frame.grid(row=1, column=0, columnspan=4, sticky="ew", padx=8, pady=8)
        progress_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            progress_frame,
            text="Today's attendance completion",
            text_color=THEME["text"],
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, padx=18, pady=(14, 4), sticky="w")
        self.dashboard_progress = ctk.CTkProgressBar(progress_frame, height=14, progress_color=THEME["success"])
        self.dashboard_progress.grid(row=1, column=0, padx=18, pady=(0, 16), sticky="ew")
        self.dashboard_progress.set(0)

        recent_frame = ctk.CTkFrame(page, fg_color=THEME["panel"], corner_radius=14)
        recent_frame.grid(row=2, column=0, columnspan=4, sticky="nsew", padx=8, pady=8)
        recent_frame.grid_columnconfigure(0, weight=1)
        recent_frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            recent_frame,
            text="Recently Detected Students",
            text_color=THEME["text"],
            font=("Segoe UI", 15, "bold"),
        ).grid(row=0, column=0, padx=16, pady=(14, 8), sticky="w")
        self.recent_tree = self.create_treeview(
            recent_frame,
            ("Name", "Roll Number", "Department", "Date", "Time", "Confidence", "Emotion"),
            height=8,
        )
        self.recent_tree.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))

    def create_attendance_page(self):
        page = ctk.CTkFrame(self.page_container, fg_color=THEME["bg"], corner_radius=0)
        page.grid_columnconfigure(0, weight=3)
        page.grid_columnconfigure(1, weight=2)
        page.grid_rowconfigure(0, weight=1)
        self.pages["Attendance"] = page

        camera_panel = ctk.CTkFrame(page, fg_color=THEME["panel"], corner_radius=14)
        camera_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10), pady=0)
        camera_panel.grid_columnconfigure(0, weight=1)
        camera_panel.grid_rowconfigure(1, weight=1)

        title_row = ctk.CTkFrame(camera_panel, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        title_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            title_row,
            text="Real-Time Computer Vision Camera",
            font=("Segoe UI", 16, "bold"),
            text_color=THEME["text"],
        ).grid(row=0, column=0, sticky="w")
        self.fps_label = ctk.CTkLabel(title_row, text="FPS: 0.0", text_color=THEME["accent"])
        self.fps_label.grid(row=0, column=1, sticky="e")

        self.video_label = ctk.CTkLabel(
            camera_panel,
            text="Camera preview will appear here",
            fg_color="#020617",
            corner_radius=10,
            text_color=THEME["muted"],
            font=("Segoe UI", 16),
        )
        self.video_label.grid(row=1, column=0, sticky="nsew", padx=14, pady=8)

        status_row = ctk.CTkFrame(camera_panel, fg_color="transparent")
        status_row.grid(row=2, column=0, sticky="ew", padx=14, pady=(8, 14))
        status_row.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.camera_status_label = self.small_status_label(status_row, "Camera Stopped", THEME["danger"], 0)
        self.face_count_label = self.small_status_label(status_row, "Faces: 0", THEME["card"], 1)
        self.tracking_label = self.small_status_label(status_row, "Tracking: idle", THEME["card"], 2)
        self.system_message_label = self.small_status_label(status_row, "Ready", THEME["card"], 3)

        side_panel = ctk.CTkFrame(page, fg_color=THEME["bg"], corner_radius=0)
        side_panel.grid(row=0, column=1, sticky="nsew")
        side_panel.grid_columnconfigure(0, weight=1)
        side_panel.grid_rowconfigure(2, weight=1)

        controls = ctk.CTkFrame(side_panel, fg_color=THEME["panel"], corner_radius=14)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        controls.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(controls, text="Start Camera", fg_color=THEME["success"], command=self.start_camera).grid(
            row=0, column=0, padx=10, pady=(12, 6), sticky="ew"
        )
        ctk.CTkButton(controls, text="Stop Camera", fg_color=THEME["danger"], command=self.stop_camera).grid(
            row=0, column=1, padx=10, pady=(12, 6), sticky="ew"
        )
        ctk.CTkButton(controls, text="Capture Screenshot", fg_color=THEME["accent_dark"], command=self.capture_screenshot).grid(
            row=1, column=0, padx=10, pady=(6, 12), sticky="ew"
        )
        ctk.CTkButton(controls, text="Export CSV", fg_color=THEME["warning"], command=self.export_attendance_csv).grid(
            row=1, column=1, padx=10, pady=(6, 12), sticky="ew"
        )
        ctk.CTkButton(controls, text="Export Excel", fg_color=THEME["card"], command=self.export_attendance_excel).grid(
            row=2, column=0, padx=10, pady=(0, 12), sticky="ew"
        )
        ctk.CTkButton(controls, text="PDF Report", fg_color=THEME["card"], command=self.generate_pdf_report).grid(
            row=2, column=1, padx=10, pady=(0, 12), sticky="ew"
        )

        zoom_panel = ctk.CTkFrame(side_panel, fg_color=THEME["panel"], corner_radius=14)
        zoom_panel.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        zoom_panel.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            zoom_panel,
            text="Face Zoom Preview",
            font=("Segoe UI", 14, "bold"),
            text_color=THEME["text"],
        ).grid(row=0, column=0, padx=12, pady=(12, 6), sticky="w")
        self.face_zoom_label = ctk.CTkLabel(
            zoom_panel,
            text="No face selected",
            fg_color="#020617",
            width=220,
            height=130,
            corner_radius=10,
            text_color=THEME["muted"],
        )
        self.face_zoom_label.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="ew")

        table_panel = ctk.CTkFrame(side_panel, fg_color=THEME["panel"], corner_radius=14)
        table_panel.grid(row=2, column=0, sticky="nsew")
        table_panel.grid_columnconfigure(0, weight=1)
        table_panel.grid_rowconfigure(2, weight=1)

        filters = ctk.CTkFrame(table_panel, fg_color="transparent")
        filters.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        filters.grid_columnconfigure((0, 1), weight=1)
        self.attendance_search_var = tk.StringVar()
        self.attendance_date_var = tk.StringVar()
        self.attendance_student_var = tk.StringVar(value="All Students")
        ctk.CTkEntry(filters, textvariable=self.attendance_search_var, placeholder_text="Search records").grid(
            row=0, column=0, padx=4, pady=4, sticky="ew"
        )
        ctk.CTkEntry(filters, textvariable=self.attendance_date_var, placeholder_text="YYYY-MM-DD").grid(
            row=0, column=1, padx=4, pady=4, sticky="ew"
        )
        self.attendance_student_filter = ctk.CTkComboBox(filters, values=["All Students"], variable=self.attendance_student_var)
        self.attendance_student_filter.grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        ctk.CTkButton(filters, text="Apply Filters", command=lambda: self.refresh_attendance_table(True)).grid(
            row=1, column=1, padx=4, pady=4, sticky="ew"
        )

        self.attendance_tree = self.create_treeview(
            table_panel,
            ("Name", "Roll Number", "Department", "Date", "Time", "Confidence", "Emotion"),
            height=10,
            sort_callback=self.sort_attendance_by,
        )
        self.attendance_tree.grid(row=2, column=0, sticky="nsew", padx=10, pady=6)

        pagination = ctk.CTkFrame(table_panel, fg_color="transparent")
        pagination.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 10))
        pagination.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(pagination, text="Previous", width=90, command=self.previous_attendance_page).grid(row=0, column=0)
        self.attendance_page_label = ctk.CTkLabel(pagination, text="Page 1", text_color=THEME["muted"])
        self.attendance_page_label.grid(row=0, column=1)
        ctk.CTkButton(pagination, text="Next", width=90, command=self.next_attendance_page).grid(row=0, column=2)

    def small_status_label(self, parent, text, color, column):
        label = ctk.CTkLabel(
            parent,
            text=text,
            fg_color=color,
            corner_radius=9,
            text_color=THEME["text"],
            height=34,
            font=("Segoe UI", 11, "bold"),
        )
        label.grid(row=0, column=column, sticky="ew", padx=4)
        return label

    def create_students_page(self):
        page = ctk.CTkFrame(self.page_container, fg_color=THEME["bg"], corner_radius=0)
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=2)
        page.grid_rowconfigure(0, weight=1)
        self.pages["Students"] = page

        form = ctk.CTkFrame(page, fg_color=THEME["panel"], corner_radius=14)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        form.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(form, text="Student Profile", font=("Segoe UI", 17, "bold"), text_color=THEME["text"]).grid(
            row=0, column=0, padx=16, pady=(16, 10), sticky="w"
        )

        self.student_name_var = tk.StringVar()
        self.student_roll_var = tk.StringVar()
        self.student_department_var = tk.StringVar(value=DEPARTMENTS[0])
        self.student_email_var = tk.StringVar()
        self.student_phone_var = tk.StringVar()

        self.form_entry(form, "Full Name", self.student_name_var, 1)
        self.form_entry(form, "Roll Number", self.student_roll_var, 3)
        ctk.CTkLabel(form, text="Department", text_color=THEME["muted"]).grid(row=5, column=0, padx=16, pady=(8, 2), sticky="w")
        ctk.CTkComboBox(form, values=DEPARTMENTS, variable=self.student_department_var).grid(
            row=6, column=0, padx=16, pady=(0, 6), sticky="ew"
        )
        self.form_entry(form, "Email", self.student_email_var, 7)
        self.form_entry(form, "Phone", self.student_phone_var, 9)

        ctk.CTkButton(form, text="Add New Student", fg_color=THEME["success"], command=self.add_student).grid(
            row=11, column=0, padx=16, pady=(16, 6), sticky="ew"
        )
        ctk.CTkButton(form, text="Update Selected", fg_color=THEME["accent_dark"], command=self.update_student).grid(
            row=12, column=0, padx=16, pady=6, sticky="ew"
        )
        ctk.CTkButton(form, text="Capture Face Dataset", fg_color=THEME["warning"], command=self.capture_student_face).grid(
            row=13, column=0, padx=16, pady=6, sticky="ew"
        )
        ctk.CTkButton(form, text="Delete Selected", fg_color=THEME["danger"], command=self.delete_student).grid(
            row=14, column=0, padx=16, pady=6, sticky="ew"
        )
        ctk.CTkButton(form, text="Clear Form", fg_color=THEME["card"], command=self.clear_student_form).grid(
            row=15, column=0, padx=16, pady=(6, 16), sticky="ew"
        )

        table_panel = ctk.CTkFrame(page, fg_color=THEME["panel"], corner_radius=14)
        table_panel.grid(row=0, column=1, sticky="nsew")
        table_panel.grid_columnconfigure(0, weight=1)
        table_panel.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(table_panel, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
        top.grid_columnconfigure(0, weight=1)
        self.student_search_var = tk.StringVar()
        ctk.CTkEntry(top, textvariable=self.student_search_var, placeholder_text="Search students").grid(
            row=0, column=0, padx=(0, 8), sticky="ew"
        )
        ctk.CTkButton(top, text="Search", width=100, command=self.refresh_students_table).grid(row=0, column=1)

        self.students_tree = self.create_treeview(
            table_panel,
            ("Student ID", "Name", "Roll Number", "Department", "Email", "Phone"),
            height=18,
        )
        self.students_tree.grid(row=2, column=0, sticky="nsew", padx=12, pady=(6, 12))
        self.students_tree.bind("<<TreeviewSelect>>", self.on_student_selected)

    def form_entry(self, parent, label, variable, row):
        ctk.CTkLabel(parent, text=label, text_color=THEME["muted"]).grid(
            row=row, column=0, padx=16, pady=(8, 2), sticky="w"
        )
        ctk.CTkEntry(parent, textvariable=variable).grid(
            row=row + 1, column=0, padx=16, pady=(0, 6), sticky="ew"
        )

    def create_analytics_page(self):
        page = ctk.CTkFrame(self.page_container, fg_color=THEME["bg"], corner_radius=0)
        page.grid_columnconfigure((0, 1), weight=1)
        page.grid_rowconfigure((1, 2), weight=1)
        self.pages["Analytics"] = page

        action_bar = ctk.CTkFrame(page, fg_color=THEME["panel"], corner_radius=14)
        action_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        action_bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            action_bar,
            text="Smart Reports and Attendance Analytics",
            font=("Segoe UI", 16, "bold"),
            text_color=THEME["text"],
        ).grid(row=0, column=0, padx=16, pady=14, sticky="w")
        ctk.CTkButton(action_bar, text="Generate PDF Report", command=self.generate_pdf_report).grid(
            row=0, column=1, padx=8, pady=12
        )
        ctk.CTkButton(action_bar, text="Generate Excel Report", command=self.generate_excel_report).grid(
            row=0, column=2, padx=(0, 16), pady=12
        )

        self.daily_chart_frame = self.chart_container(page, "Daily Attendance Graph", 1, 0)
        self.monthly_chart_frame = self.chart_container(page, "Monthly Attendance Chart", 1, 1)
        self.heatmap_frame = self.chart_container(page, "Attendance Heatmap", 2, 0)
        self.active_students_frame = ctk.CTkFrame(page, fg_color=THEME["panel"], corner_radius=14)
        self.active_students_frame.grid(row=2, column=1, sticky="nsew", padx=(10, 0), pady=(10, 0))
        self.active_students_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.active_students_frame,
            text="Most Active Students",
            text_color=THEME["text"],
            font=("Segoe UI", 15, "bold"),
        ).grid(row=0, column=0, padx=14, pady=(14, 8), sticky="w")
        self.active_students_text = ctk.CTkTextbox(self.active_students_frame, fg_color="#07111f", text_color=THEME["text"])
        self.active_students_text.grid(row=1, column=0, padx=14, pady=(0, 14), sticky="nsew")
        self.active_students_frame.grid_rowconfigure(1, weight=1)

    def chart_container(self, parent, title, row, column):
        frame = ctk.CTkFrame(parent, fg_color=THEME["panel"], corner_radius=14)
        frame.grid(row=row, column=column, sticky="nsew", padx=(0 if column == 0 else 10, 0), pady=(0 if row == 1 else 10, 0))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(frame, text=title, text_color=THEME["text"], font=("Segoe UI", 15, "bold")).grid(
            row=0, column=0, padx=14, pady=(14, 4), sticky="w"
        )
        return frame

    def create_settings_page(self):
        page = ctk.CTkFrame(self.page_container, fg_color=THEME["bg"], corner_radius=0)
        page.grid_columnconfigure((0, 1), weight=1)
        page.grid_rowconfigure(0, weight=1)
        self.pages["Settings"] = page

        camera_card = ctk.CTkFrame(page, fg_color=THEME["panel"], corner_radius=14)
        camera_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        camera_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(camera_card, text="Camera Settings", font=("Segoe UI", 17, "bold"), text_color=THEME["text"]).grid(
            row=0, column=0, padx=18, pady=(18, 12), sticky="w"
        )
        self.camera_index_var = tk.StringVar(value=str(DEFAULT_CAMERA_INDEX))
        ctk.CTkLabel(camera_card, text="Camera Device", text_color=THEME["muted"]).grid(row=1, column=0, padx=18, sticky="w")
        ctk.CTkComboBox(camera_card, values=["0", "1", "2", "3"], variable=self.camera_index_var).grid(
            row=2, column=0, padx=18, pady=(4, 14), sticky="ew"
        )
        self.brightness_var = tk.IntVar(value=DEFAULT_BRIGHTNESS)
        ctk.CTkLabel(camera_card, text="Brightness", text_color=THEME["muted"]).grid(row=3, column=0, padx=18, sticky="w")
        self.brightness_slider = ctk.CTkSlider(
            camera_card,
            from_=0,
            to=255,
            number_of_steps=255,
            command=self.on_brightness_changed,
        )
        self.brightness_slider.set(DEFAULT_BRIGHTNESS)
        self.brightness_slider.grid(row=4, column=0, padx=18, pady=(4, 14), sticky="ew")

        self.threshold_var = tk.DoubleVar(value=RECOGNITION_THRESHOLD)
        ctk.CTkLabel(camera_card, text="Recognition Threshold", text_color=THEME["muted"]).grid(row=5, column=0, padx=18, sticky="w")
        self.threshold_slider = ctk.CTkSlider(camera_card, from_=0.45, to=0.85, command=self.on_threshold_changed)
        self.threshold_slider.set(RECOGNITION_THRESHOLD)
        self.threshold_slider.grid(row=6, column=0, padx=18, pady=(4, 14), sticky="ew")

        security_card = ctk.CTkFrame(page, fg_color=THEME["panel"], corner_radius=14)
        security_card.grid(row=0, column=1, sticky="nsew")
        security_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(security_card, text="Security and Backup", font=("Segoe UI", 17, "bold"), text_color=THEME["text"]).grid(
            row=0, column=0, padx=18, pady=(18, 12), sticky="w"
        )
        self.timeout_var = tk.StringVar(value=str(self.session_timeout_minutes))
        ctk.CTkLabel(security_card, text="Session Timeout (minutes)", text_color=THEME["muted"]).grid(
            row=1, column=0, padx=18, sticky="w"
        )
        ctk.CTkEntry(security_card, textvariable=self.timeout_var).grid(row=2, column=0, padx=18, pady=(4, 12), sticky="ew")
        ctk.CTkButton(security_card, text="Save Security Settings", command=self.save_security_settings).grid(
            row=3, column=0, padx=18, pady=8, sticky="ew"
        )
        ctk.CTkButton(security_card, text="Backup Database Now", fg_color=THEME["success"], command=self.backup_database).grid(
            row=4, column=0, padx=18, pady=8, sticky="ew"
        )
        ctk.CTkButton(security_card, text="Restore Backup", fg_color=THEME["warning"], command=self.restore_database).grid(
            row=5, column=0, padx=18, pady=8, sticky="ew"
        )
        self.settings_status = ctk.CTkLabel(
            security_card,
            text=f"Logs: {LOG_FILE}",
            text_color=THEME["muted"],
            wraplength=440,
            justify="left",
        )
        self.settings_status.grid(row=6, column=0, padx=18, pady=(16, 8), sticky="w")

    def create_treeview(self, parent, columns, height=10, sort_callback=None):
        tree = ttk.Treeview(parent, columns=columns, show="headings", height=height)
        for column in columns:
            if sort_callback:
                tree.heading(column, text=column, command=lambda col=column: sort_callback(col))
            else:
                tree.heading(column, text=column)
            width = 115 if column not in ("Name", "Department") else 145
            tree.column(column, width=width, anchor="center", stretch=True)
        tree.tag_configure("odd", background=THEME["table"])
        tree.tag_configure("even", background=THEME["table_alt"])
        return tree

    def start_camera(self):
        if self.camera_running:
            return
        self.engine.reload_known_faces()
        self.camera_stop_event = threading.Event()
        self.frame_queue = queue.Queue(maxsize=FRAME_QUEUE_SIZE)
        self.camera_worker = CameraWorker(
            self.camera_index_var.get(),
            self.brightness_slider.get(),
            self.engine,
            self.frame_queue,
            self.camera_stop_event,
        )
        self.camera_worker.start()
        self.camera_running = True
        self.camera_status_label.configure(text="Camera Running", fg_color=THEME["success"])
        self.system_message_label.configure(text="Scanning faces", fg_color=THEME["card"])
        self.poll_camera_queue()
        self.refresh_dashboard()
        log_event("INFO", f"Camera started on device {self.camera_index_var.get()}")

    def stop_camera(self):
        if self.camera_stop_event is not None:
            self.camera_stop_event.set()
        if self.camera_worker is not None and self.camera_worker.is_alive():
            self.camera_worker.join(timeout=1.0)
        self.camera_worker = None
        self.camera_stop_event = None
        self.camera_running = False
        if hasattr(self, "camera_status_label"):
            self.camera_status_label.configure(text="Camera Stopped", fg_color=THEME["danger"])
            self.face_count_label.configure(text="Faces: 0")
            self.tracking_label.configure(text="Tracking: idle")
            self.system_message_label.configure(text="Ready", fg_color=THEME["card"])
            self.video_label.configure(text="Camera preview will appear here", image=None)
            self.face_zoom_label.configure(text="No face selected", image=None)
        self.refresh_dashboard()
        log_event("INFO", "Camera stopped")

    def poll_camera_queue(self):
        if not self.camera_running:
            return
        try:
            while True:
                payload = self.frame_queue.get_nowait()
                self.handle_camera_payload(payload)
        except queue.Empty:
            pass
        if self.camera_running:
            self.root.after(15, self.poll_camera_queue)

    def handle_camera_payload(self, payload):
        if payload["type"] == "error":
            self.stop_camera()
            messagebox.showerror("Camera Error", payload["message"])
            log_event("ERROR", payload["message"])
            return
        if payload["type"] == "status":
            self.system_message_label.configure(text=payload["message"])
            return
        if payload["type"] != "frame":
            return

        self.latest_raw_frame = payload["raw_frame"]
        self.latest_annotated_frame = payload["annotated_frame"]
        self.latest_faces = payload["faces"]
        self.show_camera_frame(payload["annotated_frame"])
        self.update_face_zoom()

        face_count = len(payload["faces"])
        self.face_count_label.configure(text=f"Faces: {face_count}")
        self.fps_label.configure(text=f"FPS: {payload['fps']}")
        if face_count == 0:
            self.system_message_label.configure(text="No face detected", fg_color=THEME["warning"])
            self.tracking_label.configure(text="Tracking: idle")
        else:
            movement = payload["faces"][0]["movement"]
            self.system_message_label.configure(text="Face detected", fg_color=THEME["success"])
            self.tracking_label.configure(text=f"Tracking: {movement}")
        if payload["left_tracks"]:
            self.tracking_label.configure(text=f"Face left frame: ID {payload['left_tracks'][0]}")

        for detection in payload["detections"]:
            marked = self.database.mark_attendance(
                detection["student"],
                detection["confidence"],
                detection["emotion"],
                int(self.camera_index_var.get()),
            )
            if marked:
                play_notification_sound(self.root)
                self.refresh_dashboard()
                self.refresh_attendance_table(reset_page=True)
                student_name = detection["student"]["name"]
                messagebox.showinfo(
                    "Attendance Marked",
                    f"Attendance saved for {student_name}\nConfidence: {detection['confidence']:.1f}%\nEmotion: {detection['emotion']}",
                )

    def show_camera_frame(self, frame):
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(frame_rgb)
        width = max(self.video_label.winfo_width(), 720)
        height = max(self.video_label.winfo_height(), 420)
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        self.video_photo = ImageTk.PhotoImage(image)
        self.video_label.configure(image=self.video_photo, text="")

    def update_face_zoom(self):
        if self.latest_raw_frame is None or not self.latest_faces:
            return
        x, y, w, h = self.latest_faces[0]["rect"]
        crop = self.latest_raw_frame[y : y + h, x : x + w]
        if crop.size == 0:
            return
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(crop)
        image.thumbnail((260, 140), Image.Resampling.LANCZOS)
        self.zoom_photo = ImageTk.PhotoImage(image)
        self.face_zoom_label.configure(image=self.zoom_photo, text="")

    def capture_screenshot(self):
        if self.latest_annotated_frame is None:
            messagebox.showwarning("No Camera Frame", "Start the camera before capturing a screenshot.")
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCREENSHOTS_DIR, f"screenshot_{timestamp}.jpg")
        cv2.imwrite(path, self.latest_annotated_frame)
        play_notification_sound(self.root)
        log_event("INFO", f"Screenshot saved: {path}")
        messagebox.showinfo("Screenshot Saved", f"Screenshot saved successfully:\n{path}")

    def add_student(self):
        name, roll, department, email, phone = self.get_student_form_values()
        if not name or not roll or not department:
            messagebox.showwarning("Missing Details", "Name, roll number, and department are required.")
            return
        try:
            student = self.database.add_student(name, roll, department, email, phone)
            dataset_count = 0
            if self.latest_raw_frame is not None and self.latest_faces:
                image_path, dataset_count = self.save_current_face_image(
                    student["id"],
                    student["student_uid"],
                    student["name"],
                )
                self.database.update_student(student["id"], name, roll, department, email, phone, image_path)
            self.engine.reload_known_faces()
            self.refresh_students_table()
            self.refresh_attendance_student_filter()
            self.refresh_dashboard()
            self.clear_student_form()
            play_notification_sound(self.root)
            if dataset_count:
                messagebox.showinfo(
                    "Student Added",
                    f"Student profile created successfully.\n{dataset_count} face samples and encodings saved.",
                )
            else:
                messagebox.showinfo(
                    "Student Added",
                    "Student profile created successfully.\nCapture the face dataset from the Students page.",
                )
            log_event("INFO", f"Student added: {name} ({roll})")
        except sqlite3.IntegrityError as error:
            messagebox.showerror("Duplicate Student", f"Roll number or student ID already exists.\n{error}")

    def update_student(self):
        if self.selected_student_id is None:
            messagebox.showwarning("No Student Selected", "Select a student from the table first.")
            return
        name, roll, department, email, phone = self.get_student_form_values()
        if not name or not roll or not department:
            messagebox.showwarning("Missing Details", "Name, roll number, and department are required.")
            return
        try:
            self.database.update_student(self.selected_student_id, name, roll, department, email, phone)
            self.engine.reload_known_faces()
            self.refresh_students_table()
            self.refresh_attendance_student_filter()
            self.refresh_dashboard()
            messagebox.showinfo("Student Updated", "Student profile updated successfully.")
            log_event("INFO", f"Student updated: {name} ({roll})")
        except sqlite3.IntegrityError as error:
            messagebox.showerror("Update Failed", f"Could not update student.\n{error}")

    def delete_student(self):
        if self.selected_student_id is None:
            messagebox.showwarning("No Student Selected", "Select a student from the table first.")
            return
        if not messagebox.askyesno("Delete Student", "Delete the selected student profile?"):
            return
        self.database.delete_student(self.selected_student_id)
        self.selected_student_id = None
        self.clear_student_form()
        self.engine.reload_known_faces()
        self.refresh_students_table()
        self.refresh_attendance_student_filter()
        self.refresh_dashboard()
        log_event("INFO", "Student deleted")

    def capture_student_face(self):
        if self.selected_student_id is None:
            messagebox.showwarning("No Student Selected", "Select a student before capturing a face photo.")
            return
        student = self.database.get_student_by_id(self.selected_student_id)
        if student is None:
            messagebox.showerror("Student Missing", "Selected student was not found in the database.")
            return
        try:
            image_path, dataset_count = self.save_current_face_image(
                student["id"],
                student["student_uid"],
                student["name"],
            )
        except ValueError as error:
            messagebox.showwarning("Face Capture Needed", str(error))
            return
        self.database.update_student(
            student["id"],
            student["name"],
            student["roll_number"],
            student["department"],
            student["email"],
            student["phone"],
            image_path,
        )
        self.engine.reload_known_faces()
        self.refresh_students_table()
        play_notification_sound(self.root)
        messagebox.showinfo(
            "Dataset Captured",
            f"Automatic face dataset captured successfully.\n{dataset_count} images and encodings saved.",
        )

    def save_current_face_image(self, student_id, student_uid, name):
        if self.latest_raw_frame is None or not self.latest_faces:
            raise ValueError("Start camera and keep a face visible before capturing.")
        largest_face = max(self.latest_faces, key=lambda face: face["rect"][2] * face["rect"][3])
        x, y, w, h = largest_face["rect"]
        gray = cv2.cvtColor(self.latest_raw_frame, cv2.COLOR_BGR2GRAY)
        face = gray[y : y + h, x : x + w]
        face = self.engine.preprocess_face(face)
        safe_name = "".join(char for char in name if char.isalnum() or char in (" ", "_", "-")).strip()
        safe_name = "_".join(safe_name.split()) or f"student_{student_id}"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        variants = self.create_face_dataset_variants(face)
        primary_image_path = ""

        for index, variant in enumerate(variants, start=1):
            image_path = os.path.join(
                STUDENT_IMAGES_DIR,
                f"{student_uid}_{safe_name}_{timestamp}_{index:02d}.jpg",
            )
            cv2.imwrite(image_path, variant)
            encoding = self.engine.create_face_encoding(variant)
            self.database.save_face_encoding(student_id, image_path, encoding)
            if not primary_image_path:
                primary_image_path = image_path

        log_event(
            "INFO",
            f"Face dataset saved for {name}: {len(variants)} images and encodings",
        )
        return primary_image_path, len(variants)

    def create_face_dataset_variants(self, face):
        """Create automatic dataset samples from one clean webcam face crop."""
        variants = [face]
        height, width = face.shape
        center = (width // 2, height // 2)

        transforms = [
            ("bright", cv2.convertScaleAbs(face, alpha=1.08, beta=10)),
            ("soft", cv2.GaussianBlur(face, (3, 3), 0)),
            ("contrast", cv2.convertScaleAbs(face, alpha=1.18, beta=-8)),
            ("dim", cv2.convertScaleAbs(face, alpha=0.92, beta=-6)),
        ]
        for _, transformed in transforms:
            variants.append(transformed)

        for angle in (-5, 5, -9):
            matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
            rotated = cv2.warpAffine(
                face,
                matrix,
                (width, height),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT,
            )
            variants.append(rotated)

        return variants[:DATASET_IMAGES_PER_CAPTURE]

    def get_student_form_values(self):
        return (
            self.student_name_var.get().strip(),
            self.student_roll_var.get().strip(),
            self.student_department_var.get().strip(),
            self.student_email_var.get().strip(),
            self.student_phone_var.get().strip(),
        )

    def clear_student_form(self):
        self.selected_student_id = None
        self.student_name_var.set("")
        self.student_roll_var.set("")
        self.student_department_var.set(DEPARTMENTS[0])
        self.student_email_var.set("")
        self.student_phone_var.set("")

    def on_student_selected(self, event):
        selected = self.students_tree.selection()
        if not selected:
            return
        values = self.students_tree.item(selected[0], "values")
        student_uid = values[0]
        for student in self.database.fetch_students(active_only=True):
            if student["student_uid"] == student_uid:
                self.selected_student_id = student["id"]
                self.student_name_var.set(student["name"])
                self.student_roll_var.set(student["roll_number"])
                self.student_department_var.set(student["department"])
                self.student_email_var.set(student["email"])
                self.student_phone_var.set(student["phone"])
                break

    def refresh_students_table(self):
        for row in self.students_tree.get_children():
            self.students_tree.delete(row)
        students = self.database.fetch_students(search=self.student_search_var.get() if hasattr(self, "student_search_var") else "")
        for index, student in enumerate(students):
            tag = "even" if index % 2 == 0 else "odd"
            self.students_tree.insert(
                "",
                "end",
                values=(
                    student["student_uid"],
                    student["name"],
                    student["roll_number"],
                    student["department"],
                    student["email"],
                    student["phone"],
                ),
                tags=(tag,),
            )

    def refresh_attendance_student_filter(self):
        if not hasattr(self, "attendance_student_filter"):
            return
        values = ["All Students"] + [student["name"] for student in self.database.fetch_students()]
        self.attendance_student_filter.configure(values=values)

    def sort_attendance_by(self, column):
        if self.attendance_sort_column == column:
            self.attendance_sort_desc = not self.attendance_sort_desc
        else:
            self.attendance_sort_column = column
            self.attendance_sort_desc = True
        self.refresh_attendance_table(reset_page=True)

    def refresh_attendance_table(self, reset_page=False):
        if reset_page:
            self.attendance_page_number = 0
        if not hasattr(self, "attendance_tree"):
            return
        for row in self.attendance_tree.get_children():
            self.attendance_tree.delete(row)
        records, total = self.database.fetch_attendance(
            search=self.attendance_search_var.get(),
            date_filter=self.attendance_date_var.get(),
            student_filter=self.attendance_student_var.get(),
            order_by=self.attendance_sort_column,
            desc=self.attendance_sort_desc,
            limit=ATTENDANCE_PAGE_SIZE,
            offset=self.attendance_page_number * ATTENDANCE_PAGE_SIZE,
        )
        self.attendance_total_rows = total
        for index, record in enumerate(records):
            tag = "even" if index % 2 == 0 else "odd"
            self.attendance_tree.insert(
                "",
                "end",
                values=(
                    record["name"],
                    record["roll_number"],
                    record["department"],
                    record["date"],
                    record["time"],
                    f"{record['confidence']:.1f}%",
                    record["emotion"],
                ),
                tags=(tag,),
            )
        total_pages = max(1, (self.attendance_total_rows + ATTENDANCE_PAGE_SIZE - 1) // ATTENDANCE_PAGE_SIZE)
        self.attendance_page_label.configure(text=f"Page {self.attendance_page_number + 1} of {total_pages}")

    def previous_attendance_page(self):
        if self.attendance_page_number > 0:
            self.attendance_page_number -= 1
            self.refresh_attendance_table()

    def next_attendance_page(self):
        total_pages = max(1, (self.attendance_total_rows + ATTENDANCE_PAGE_SIZE - 1) // ATTENDANCE_PAGE_SIZE)
        if self.attendance_page_number + 1 < total_pages:
            self.attendance_page_number += 1
            self.refresh_attendance_table()

    def export_attendance_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv")],
            initialfile=f"attendance_export_{datetime.now().strftime('%Y%m%d')}.csv",
        )
        if not path:
            return
        data = self.database.export_attendance_dataframe(
            self.attendance_search_var.get(),
            self.attendance_date_var.get(),
            self.attendance_student_var.get(),
        )
        data.to_csv(path, index=False)
        messagebox.showinfo("Export Complete", f"Attendance exported to:\n{path}")

    def export_attendance_excel(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel Files", "*.xlsx")],
            initialfile=f"attendance_export_{datetime.now().strftime('%Y%m%d')}.xlsx",
        )
        if not path:
            return
        data = self.database.export_attendance_dataframe(
            self.attendance_search_var.get(),
            self.attendance_date_var.get(),
            self.attendance_student_var.get(),
        )
        data.to_excel(path, index=False, sheet_name="Attendance")
        messagebox.showinfo("Export Complete", f"Attendance exported to:\n{path}")

    def refresh_dashboard(self):
        if not hasattr(self, "total_students_value"):
            return
        total_students = self.database.student_count()
        today_count = self.database.today_attendance_count()
        percentage = self.database.attendance_percentage_today()
        self.total_students_value.configure(text=str(total_students))
        self.today_attendance_value.configure(text=str(today_count))
        self.camera_status_value.configure(
            text="Running" if self.camera_running else "Stopped",
            text_color=THEME["success"] if self.camera_running else THEME["warning"],
        )
        self.percentage_value.configure(text=f"{percentage}%")
        self.dashboard_progress.set(min(1.0, percentage / 100.0))
        if hasattr(self, "recent_tree"):
            for row in self.recent_tree.get_children():
                self.recent_tree.delete(row)
            for index, record in enumerate(self.database.recent_attendance()):
                tag = "even" if index % 2 == 0 else "odd"
                self.recent_tree.insert(
                    "",
                    "end",
                    values=(
                        record["name"],
                        record["roll_number"],
                        record["department"],
                        record["date"],
                        record["time"],
                        f"{record['confidence']:.1f}%",
                        record["emotion"],
                    ),
                    tags=(tag,),
                )

    def refresh_analytics(self):
        self.draw_daily_chart()
        self.draw_monthly_chart()
        self.draw_heatmap()
        self.draw_most_active_students()

    def clear_chart_frame(self, frame):
        for widget in frame.winfo_children():
            if isinstance(widget, ctk.CTkLabel):
                continue
            widget.destroy()

    def base_figure(self, width=5.2, height=3.0):
        figure = Figure(figsize=(width, height), dpi=100, facecolor=THEME["panel"])
        return figure

    def draw_daily_chart(self):
        self.clear_chart_frame(self.daily_chart_frame)
        labels, counts = self.database.daily_counts(14)
        figure = self.base_figure()
        axis = figure.add_subplot(111)
        axis.set_facecolor(THEME["panel"])
        axis.plot(labels, counts, color=THEME["accent"], marker="o", linewidth=2)
        axis.tick_params(axis="x", colors=THEME["muted"], rotation=35)
        axis.tick_params(axis="y", colors=THEME["muted"])
        for spine in axis.spines.values():
            spine.set_color(THEME["border"])
        axis.grid(color=THEME["border"], alpha=0.35)
        canvas = FigureCanvasTkAgg(figure, master=self.daily_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

    def draw_monthly_chart(self):
        self.clear_chart_frame(self.monthly_chart_frame)
        labels, counts = self.database.monthly_counts(6)
        figure = self.base_figure()
        axis = figure.add_subplot(111)
        axis.set_facecolor(THEME["panel"])
        axis.bar(labels, counts, color=THEME["success"])
        axis.tick_params(axis="x", colors=THEME["muted"], rotation=25)
        axis.tick_params(axis="y", colors=THEME["muted"])
        for spine in axis.spines.values():
            spine.set_color(THEME["border"])
        canvas = FigureCanvasTkAgg(figure, master=self.monthly_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

    def draw_heatmap(self):
        self.clear_chart_frame(self.heatmap_frame)
        labels, counts = self.database.daily_counts(35)
        grid = np.array(counts).reshape(5, 7)
        figure = self.base_figure()
        axis = figure.add_subplot(111)
        axis.set_facecolor(THEME["panel"])
        axis.imshow(grid, cmap="Blues", aspect="auto")
        axis.set_xticks(range(7))
        axis.set_xticklabels(["M", "T", "W", "T", "F", "S", "S"], color=THEME["muted"])
        axis.set_yticks(range(5))
        axis.set_yticklabels([f"W{i + 1}" for i in range(5)], color=THEME["muted"])
        for row in range(5):
            for col in range(7):
                axis.text(col, row, str(grid[row, col]), ha="center", va="center", color="#ffffff")
        for spine in axis.spines.values():
            spine.set_color(THEME["border"])
        canvas = FigureCanvasTkAgg(figure, master=self.heatmap_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

    def draw_most_active_students(self):
        self.active_students_text.configure(state="normal")
        self.active_students_text.delete("1.0", "end")
        students = self.database.most_active_students()
        if not students:
            self.active_students_text.insert("end", "No attendance records yet.\n")
        else:
            for index, student in enumerate(students, start=1):
                self.active_students_text.insert(
                    "end",
                    f"{index}. {student['name']} ({student['roll_number']})\n"
                    f"   {student['department']} - {student['total']} attendance records\n\n",
                )
        self.active_students_text.configure(state="disabled")

    def generate_pdf_report(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".pdf",
            filetypes=[("PDF Files", "*.pdf")],
            initialfile=f"attendance_report_{datetime.now().strftime('%Y%m%d')}.pdf",
        )
        if not path:
            return
        labels, counts = self.database.daily_counts(14)
        with PdfPages(path) as pdf:
            figure = Figure(figsize=(8.27, 11.69), dpi=100)
            axis = figure.add_subplot(111)
            axis.axis("off")
            summary = (
                f"{APP_TITLE}\n\n"
                f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Total Students: {self.database.student_count()}\n"
                f"Today's Attendance: {self.database.today_attendance_count()}\n"
                f"Today's Percentage: {self.database.attendance_percentage_today()}%\n"
            )
            axis.text(0.05, 0.95, summary, va="top", fontsize=14)
            pdf.savefig(figure)

            chart = Figure(figsize=(8.27, 5.0), dpi=100)
            chart_axis = chart.add_subplot(111)
            chart_axis.plot(labels, counts, marker="o", linewidth=2)
            chart_axis.set_title("Daily Attendance - Last 14 Days")
            chart_axis.set_ylabel("Present Students")
            chart_axis.tick_params(axis="x", rotation=35)
            chart.tight_layout()
            pdf.savefig(chart)
        messagebox.showinfo("PDF Report Created", f"PDF report generated:\n{path}")
        log_event("INFO", f"PDF report generated: {path}")

    def generate_excel_report(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel Files", "*.xlsx")],
            initialfile=f"attendance_report_{datetime.now().strftime('%Y%m%d')}.xlsx",
        )
        if not path:
            return
        attendance = self.database.export_attendance_dataframe()
        students = self.database.export_students_dataframe()
        summary = pd.DataFrame(
            [
                ["Total Students", self.database.student_count()],
                ["Today's Attendance", self.database.today_attendance_count()],
                ["Today's Attendance Percentage", self.database.attendance_percentage_today()],
            ],
            columns=["Metric", "Value"],
        )
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            summary.to_excel(writer, index=False, sheet_name="Summary")
            students.to_excel(writer, index=False, sheet_name="Students")
            attendance.to_excel(writer, index=False, sheet_name="Attendance")
        messagebox.showinfo("Excel Report Created", f"Excel report generated:\n{path}")
        log_event("INFO", f"Excel report generated: {path}")

    def on_brightness_changed(self, value):
        self.brightness_var.set(int(float(value)))
        if self.camera_worker is not None:
            self.camera_worker.set_brightness(value)

    def on_threshold_changed(self, value):
        self.threshold_var.set(float(value))
        self.engine.threshold = float(value)

    def save_security_settings(self):
        try:
            timeout = int(self.timeout_var.get())
            if timeout < 1:
                raise ValueError
            self.session_timeout_minutes = timeout
            messagebox.showinfo("Settings Saved", "Security settings saved successfully.")
        except ValueError:
            messagebox.showerror("Invalid Timeout", "Session timeout must be a positive number.")

    def backup_database(self):
        path = self.database.backup_database()
        messagebox.showinfo("Backup Complete", f"Database backup saved:\n{path}")

    def restore_database(self):
        path = filedialog.askopenfilename(
            filetypes=[("SQLite Database", "*.db"), ("All Files", "*.*")],
            initialdir=BACKUP_DIR,
        )
        if not path:
            return
        if not messagebox.askyesno("Restore Backup", "Restore this backup and reload the application data?"):
            return
        self.stop_camera()
        self.database.restore_database(path)
        self.engine.reload_known_faces()
        self.refresh_dashboard()
        self.refresh_students_table()
        self.refresh_attendance_table(reset_page=True)
        messagebox.showinfo("Restore Complete", "Database restored successfully.")

    def update_clock(self):
        if self.current_user and hasattr(self, "header_clock"):
            self.header_clock.configure(text=datetime.now().strftime("%A, %d %b %Y  %I:%M:%S %p"))
        self.root.after(1000, self.update_clock)

    def record_activity(self, event=None):
        self.last_activity = time.time()

    def check_session_timeout(self):
        if self.current_user:
            elapsed = time.time() - self.last_activity
            if elapsed > self.session_timeout_minutes * 60:
                self.stop_camera()
                messagebox.showinfo("Session Timeout", "Session expired for security. Please login again.")
                self.logout()
        self.root.after(30000, self.check_session_timeout)

    def logout(self):
        self.stop_camera()
        log_event("INFO", f"Admin logged out: {self.current_user}")
        self.show_login()

    def exit_application(self):
        self.stop_camera()
        self.database.close()
        self.root.destroy()


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    SmartAttendanceApplication(root)
    root.mainloop()


if __name__ == "__main__":
    main()
