from typing import TYPE_CHECKING, Optional, Text, Type

import duckdb

from languru.config import console
from languru.utils.sql import CREATE_EMBEDDING_INDEX_LINE, openapi_to_create_table_sql

if TYPE_CHECKING:
    from languru.documents.document import Document, Point


class PointQuerySet:
    def __init__(self, model: Type["Point"], *args, **kwargs):
        self.model = model
        self.__args = args
        self.__kwargs = kwargs

    def touch(self, *, conn: "duckdb.DuckDBPyConnection", debug: bool = False) -> bool:
        conn.sql("INSTALL vss;")
        conn.sql("LOAD vss;")
        create_table_sql = openapi_to_create_table_sql(
            self.model.model_json_schema(),
            table_name=self.model.TABLE_NAME,
            primary_key="point_id",
            indexes=["content_md5"],
        ).strip()
        create_table_sql = (
            create_table_sql
            + "\n"
            + CREATE_EMBEDDING_INDEX_LINE.format(
                table_name=self.model.TABLE_NAME,
                column_name="embedding",
                metric="cosine",
            )
        ).strip()

        if debug:
            console.print(
                f"Creating table: '{self.model.TABLE_NAME}' with SQL:\n"
                + f"{create_table_sql}\n"
                + "=== End of SQL ===\n"
            )
            # CREATE TABLE points (
            #     point_id TEXT,
            #     document_id TEXT NOT NULL,
            #     document_md5 TEXT NOT NULL,
            #     embedding FLOAT[512],
            #     PRIMARY KEY (point_id)
            # );
            # CREATE INDEX idx_points_embedding ON points USING HNSW(embedding) WITH (metric = 'cosine');  # noqa: E501
        conn.sql(create_table_sql)
        return True

    def retrieve(
        self,
        point_id: Text,
        *,
        conn: "duckdb.DuckDBPyConnection",
        debug: bool = False,
        with_embedding: bool = False,
    ) -> Optional["Point"]:
        columns = list(self.model.model_json_schema()["properties"].keys())
        if not with_embedding:
            columns = [c for c in columns if c != "embedding"]
        columns_expr = ",".join(columns)

        query = f"SELECT {columns_expr} FROM {self.model.TABLE_NAME} WHERE point_id = ?"
        if debug:
            console.print(f"Query: {query}")

        result = conn.execute(query, [point_id]).fetchone()

        if result is None:
            return None
        data = dict(zip([c[0] for c in columns], result))
        return self.model.model_validate(data)


class DocumentQuerySet:
    def __init__(self, model: Type["Document"], *args, **kwargs):
        self.model = model
        self.__args = args
        self.__kwargs = kwargs

    def touch(self, *, conn: "duckdb.DuckDBPyConnection", debug: bool = False) -> bool:
        create_table_sql = openapi_to_create_table_sql(
            self.model.model_json_schema(),
            table_name=self.model.TABLE_NAME,
            primary_key="document_id",
            unique_fields=["name"],
            indexes=["content_md5"],
        )
        if debug:
            console.print(
                f"Creating table: '{self.model.TABLE_NAME}' with SQL:\n"
                + f"{create_table_sql}\n"
                + "=== End of SQL ===\n"
            )
            # CREATE TABLE documents (
            #     document_id TEXT,
            #     name VARCHAR(255) NOT NULL UNIQUE,
            #     content VARCHAR(5000) NOT NULL,
            #     content_md5 TEXT NOT NULL,
            #     metadata JSON,
            #     created_at INT,
            #     updated_at INT,
            #     PRIMARY KEY (document_id)
            # );
            # CREATE INDEX idx_documents_content_md5 ON documents (content_md5);
        conn.sql(create_table_sql)

        self.model.POINT_TYPE.objects.touch(conn=conn, debug=debug)
        return True

    def retrieve(
        self,
        document_id: Text,
        *,
        conn: "duckdb.DuckDBPyConnection",
        debug: bool = False,
    ) -> Optional["Document"]:
        columns = list(self.model.model_json_schema()["properties"].keys())
        columns_expr = ",".join(columns)

        query = (
            f"SELECT {columns_expr} FROM {self.model.TABLE_NAME} WHERE document_id = ?"
        )
        if debug:
            console.print(f"Query: {query}")

        result = conn.execute(query, [document_id]).fetchone()

        if result is None:
            return None
        data = dict(zip([c[0] for c in columns], result))
        return self.model.model_validate(data)


class PointQuerySetDescriptor:
    def __get__(self, instance: None, owner: Type["Point"]) -> "PointQuerySet":
        if instance is not None:
            raise AttributeError(
                "PointQuerySetDescriptor cannot be accessed via an instance."
            )
        return PointQuerySet(owner)


class DocumentQuerySetDescriptor:
    def __get__(self, instance: None, owner: Type["Document"]) -> "DocumentQuerySet":
        if instance is not None:
            raise AttributeError(
                "DocumentQuerySetDescriptor cannot be accessed via an instance."
            )
        return DocumentQuerySet(owner)
