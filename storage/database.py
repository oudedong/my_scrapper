"""데이터베이스 연결 및 세션/초기화 관리 모듈"""
from __future__ import annotations
import sqlite3
import os

from typing import Any

__all__ = [
    "Database",
    "ContentModel",
    "UrlModel",
]


class ContentModel:
    """수집된 본문 콘텐츠 레코드 모델"""

    def __init__(
        self,
        url: str,
        title: str,
        content: str | None = None,
        content_hash: str | None = None,
        is_visited: bool = False,
    ):
        self.url: str = url
        self.title: str = title
        self.content: str | None = content
        self.content_hash: str | None = content_hash
        self.is_visited: bool = is_visited
        self.id: int | None = None
        self.db: Database | None = None

    def update(self) -> None:
        if self.db is None:
            raise ValueError("Database is not set")
        self.db.update(self)

    def delete(self) -> None:
        if self.db is None:
            raise ValueError("Database is not set")
        self.db.delete(self)


UrlModel = ContentModel


class Database:
    db_path: str

    def __init__(self, db_path: str = "scrapper.db"):
        self.db_path = db_path
        # 경로안에 디렉터리가 지정되어 있고 없으면 생성
        dirname = os.path.dirname(db_path)
        if dirname and not os.path.exists(dirname):
            os.makedirs(dirname, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        _ = self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS url_contents (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                content TEXT,
                content_hash TEXT,
                is_visited BOOLEAN NOT NULL DEFAULT FALSE
            )
        ''')
        self.conn.commit()

    def close(self) -> None:
        """DB 연결 종료"""
        self.conn.close()

    def insert(self, model: ContentModel) -> None:
        _ = self.cursor.execute('''
            INSERT OR IGNORE INTO url_contents (url, title, content, content_hash, is_visited)
            VALUES (?, ?, ?, ?, ?)
        ''', (model.url, model.title, model.content, model.content_hash, int(model.is_visited)))
        model.db = self
        if self.cursor.rowcount > 0:
            model.id = self.cursor.lastrowid
        else:
            existing = self.select_by_url(model.url)
            if existing:
                model.id = existing.id
        self.conn.commit()

    def update(self, model: ContentModel) -> None:
        if model.id is not None:
            _ = self.cursor.execute('''
                UPDATE url_contents
                SET title = ?, content = ?, content_hash = ?, is_visited = ?
                WHERE id = ?
            ''', (model.title, model.content, model.content_hash, int(model.is_visited), model.id))
        else:
            _ = self.cursor.execute('''
                UPDATE url_contents
                SET title = ?, content = ?, content_hash = ?, is_visited = ?
                WHERE url = ?
            ''', (model.title, model.content, model.content_hash, int(model.is_visited), model.url))
        self.conn.commit()

    def delete(self, model: ContentModel) -> None:
        if model.id is not None:
            _ = self.cursor.execute('''
                DELETE FROM url_contents WHERE id = ?
            ''', (model.id,))
        else:
            _ = self.cursor.execute('''
                DELETE FROM url_contents WHERE url = ?
            ''', (model.url,))
        model.db = None
        model.id = None
        self.conn.commit()

    def _select(self, row: tuple[Any, ...] | None) -> ContentModel | None:
        if row is None:
            return None
        model = ContentModel(str(row[1]), str(row[2]), row[3], row[4], is_visited=bool(row[5]))
        model.id = int(row[0])
        model.db = self
        return model

    def select_by_url(self, url: str) -> ContentModel | None:
        row = self.cursor.execute('''
            SELECT * FROM url_contents WHERE url = ?
        ''', (url,)).fetchone()
        return self._select(row)

    def select_not_visited(self, limit: int = 10) -> list[ContentModel]:
        rows = self.cursor.execute("""
            SELECT * FROM url_contents WHERE is_visited = 0 LIMIT ?
        """, (limit,)).fetchall()
        models = [self._select(row) for row in rows]
        return [m for m in models if m is not None]

    def select_not_visited_one(self) -> ContentModel | None:
        results = self.select_not_visited(1)
        return results[0] if results else None


    
