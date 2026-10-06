"""데이터베이스 연결 및 세션/초기화 관리 모듈"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
import os
import sqlite3
from typing import Any, TypeVar, overload, override

__all__ = [
    "Database",
    "Model",
    "ContentModel",
    "SourceUrlModel",
    "UrlModel",
]

M = TypeVar("M", bound="Model")


class Model(ABC):
    __table_name__: str

    def __init__(self) -> None:
        # 아래 필드는 사용자가 변경하면 안됨!
        self.db: Database | None = None
        self.id: int | None = None

    @classmethod
    @abstractmethod
    def colnames(cls) -> tuple[str, ...]:
        pass

    @property
    @abstractmethod
    def values(self) -> tuple[Any, ...]:
        pass

    def update(self) -> None:
        if self.db is None:
            raise ValueError("Database is not set")
        self.db.update(self)

    def delete(self) -> None:
        if self.db is None:
            raise ValueError("Database is not set")
        self.db.delete(self)


class SourceUrlModel(Model):
    """탐색의 시작이 될 url목록"""

    __table_name__: str = "source_urls"

    def __init__(
        self,
        url: str,
        description: str,
        lastcheck: datetime | str | None = None,
    ) -> None:
        super().__init__()
        self.url: str = url
        self.description: str = description
        if isinstance(lastcheck, str):
            try:
                self.lastcheck: datetime | None = datetime.fromisoformat(lastcheck)
            except ValueError:
                self.lastcheck = None
        else:
            self.lastcheck = lastcheck

    @classmethod
    @override
    def colnames(cls) -> tuple[str, ...]:
        return ("url", "description", "lastcheck")

    @property
    @override
    def values(self) -> tuple[object, ...]:
        lastcheck_val = (
            self.lastcheck.isoformat()
            if isinstance(self.lastcheck, datetime)
            else self.lastcheck
        )
        return (self.url, self.description, lastcheck_val)


class ContentModel(Model):
    """수집된 본문 콘텐츠 레코드 모델"""

    __table_name__: str = "url_contents"

    def __init__(
        self,
        url: str,
        title: str,
        content: str | None = None,
        content_hash: str | None = None,
        is_visited: bool = False,
    ) -> None:
        super().__init__()
        self.url: str = url
        self.title: str = title
        self.content: str | None = content
        self.content_hash: str | None = content_hash
        self.is_visited: bool = bool(is_visited)

    @classmethod
    @override
    def colnames(cls) -> tuple[str, ...]:
        return ("url", "title", "content", "content_hash", "is_visited")

    @property
    @override
    def values(self) -> tuple[object, ...]:
        return (
            self.url,
            self.title,
            self.content,
            self.content_hash,
            int(self.is_visited),
        )


UrlModel = ContentModel


class Database:
    db_path: str

    def __init__(self, db_path: str = "scrapper.db") -> None:
        self.db_path = db_path
        # 경로안에 디렉터리가 지정되어 있고 없으면 생성
        dirname = os.path.dirname(db_path)
        if dirname and not os.path.exists(dirname):
            os.makedirs(dirname, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        _ = self.cursor.executescript('''
            CREATE TABLE IF NOT EXISTS url_contents (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                content TEXT,
                content_hash TEXT,
                is_visited BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS source_urls (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL UNIQUE,
                description TEXT,
                lastcheck DATETIME
            );
        ''')
        self.conn.commit()

    def close(self) -> None:
        """DB 연결 종료"""
        self.conn.close()

    def insert(self, model: Model) -> None:
        name_str = ", ".join(model.colnames())
        ques = ", ".join(["?" for _ in model.colnames()])
        _ = self.cursor.execute(f'''
            INSERT OR IGNORE INTO {model.__table_name__} ({name_str})
            VALUES ({ques})
        ''', model.values)
        model.db = self
        if self.cursor.rowcount > 0:
            model.id = self.cursor.lastrowid
        else:
            url_val = getattr(model, "url", None)
            if url_val is not None:
                existing = self.select_by_url(model.__class__, url_val)
                if existing:
                    model.id = existing.id
        self.conn.commit()

    def update(self, model: Model) -> None:
        set_line = ", ".join([f"{name} = ?" for name in model.colnames() if name != "id"])
        if model.id is not None:
            _ = self.cursor.execute(f'''
                UPDATE {model.__table_name__}
                SET {set_line}
                WHERE id = ?
            ''', model.values + (model.id,))
        else:
            url_val = getattr(model, "url", None)
            if url_val is not None:
                _ = self.cursor.execute(f'''
                    UPDATE {model.__table_name__}
                    SET {set_line}
                    WHERE url = ?
                ''', model.values + (url_val,))
            else:
                raise ValueError("Model id and url are both None")
        self.conn.commit()

    def delete(self, model: Model) -> None:
        if model.id is not None:
            _ = self.cursor.execute(f'''
                DELETE FROM {model.__table_name__}
                WHERE id = ?
            ''', (model.id,))
        else:
            url_val = getattr(model, "url", None)
            if url_val is not None:
                _ = self.cursor.execute(f'''
                    DELETE FROM {model.__table_name__}
                    WHERE url = ?
                ''', (url_val,))
            else:
                raise ValueError("Model id and url are both None")
        model.db = None
        model.id = None
        self.conn.commit()

    def _select(self, cls: type[M], row: tuple[object, ...] | None) -> M | None:
        if row is None:
            return None
        model = cls(*row[1:])
        model.id = int(str(row[0]))
        model.db = self
        return model

    @overload
    def select_by_url(self, cls_or_url: str, url: None = None) -> ContentModel | None: ...

    @overload
    def select_by_url(self, cls_or_url: type[M], url: str) -> M | None: ...

    def select_by_url(
        self,
        cls_or_url: type[M] | str,
        url: str | None = None,
    ) -> Model | None:
        if isinstance(cls_or_url, str):
            cls: type[Model] = ContentModel
            target_url = cls_or_url
        else:
            cls = cls_or_url
            if url is None:
                raise ValueError("url must be provided when cls is specified")
            target_url = url

        row = self.cursor.execute(f'''
            SELECT * FROM {cls.__table_name__} WHERE url = ?
        ''', (target_url,)).fetchone()
        return self._select(cls, row)

    def select_not_visited(self, limit: int = 10, after_id: int = 0) -> list[ContentModel]:
        rows = self.cursor.execute("""
            SELECT * FROM url_contents WHERE is_visited = 0 AND id > ? ORDER BY id ASC LIMIT ?
        """, (after_id, limit)).fetchall()
        models = [self._select(ContentModel, row) for row in rows]
        return [m for m in models if m is not None]

    def select_not_visited_one(self) -> ContentModel | None:
        results = self.select_not_visited(1)
        return results[0] if results else None

    def select_sources(self) -> list[SourceUrlModel]:
        """등록된 모든 시작 URL(SourceUrlModel) 목록을 조회합니다."""
        rows = self.cursor.execute("SELECT * FROM source_urls").fetchall()
        models = [self._select(SourceUrlModel, row) for row in rows]
        return [m for m in models if m is not None]
