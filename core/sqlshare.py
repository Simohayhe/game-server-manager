"""サーバー間データ共有用MySQLグループ管理。

「グループ」= 共有データベース1つ + 専用MySQLユーザー1つ。
グループにゲームサーバーを追加すると、そのサーバーのIPからだけ
接続できるユーザー定義(user@ip)が発行される。
プラグイン(LuckPerms等)には接続情報(host/db/user/password)を設定する。

グループのパスワードとメンバー一覧はグループDB内の _gsm_meta テーブルに保持する
(MySQL自体が唯一の状態置き場。アプリ側にファイルは持たない)。
"""
from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass, field

META_TABLE = "_gsm_meta"


@dataclass
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    prefix: str = "gsdata_"


@dataclass
class GroupInfo:
    name: str            # グループ名(prefixなし)
    database: str        # 実DB名(prefixあり)
    members: list[str] = field(default_factory=list)  # 参加サーバーのIP


class SqlShareError(Exception):
    pass


class SqlShareManager:
    def __init__(self, cfg: MySQLConfig):
        self.cfg = cfg

    # ---------- 内部 ----------

    def _connect(self, database: str | None = None):
        import pymysql  # 遅延import(SQLタブ未使用時は不要なため)
        try:
            return pymysql.connect(
                host=self.cfg.host, port=self.cfg.port,
                user=self.cfg.user, password=self.cfg.password,
                database=database, autocommit=True, connect_timeout=8)
        except Exception as exc:
            raise SqlShareError(f"MySQL({self.cfg.host})に接続できません: {exc}") from exc

    def _dbname(self, group: str) -> str:
        if not re.fullmatch(r"[a-z0-9_]{1,24}", group):
            raise SqlShareError(
                "グループ名は英小文字・数字・_のみ、24文字以内にしてください")
        return f"{self.cfg.prefix}{group}"

    @staticmethod
    def _quote_ident(name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def _meta_get(self, cur, db: str, key: str) -> str | None:
        cur.execute(
            f"SELECT v FROM {self._quote_ident(db)}.{META_TABLE} WHERE k = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    # ---------- グループ操作 ----------

    def list_groups(self) -> list[GroupInfo]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SHOW DATABASES LIKE %s", (self.cfg.prefix.replace("_", r"\_") + "%",))
            dbs = [r[0] for r in cur.fetchall()]
            groups = []
            for db in dbs:
                name = db[len(self.cfg.prefix):]
                members: list[str] = []
                try:
                    cur.execute(
                        f"SELECT k FROM {self._quote_ident(db)}.{META_TABLE}"
                        " WHERE k LIKE 'member:%'")
                    members = [r[0].split(":", 1)[1] for r in cur.fetchall()]
                except Exception:
                    pass  # メタテーブルなし(手動作成DB等)は空メンバー扱い
                groups.append(GroupInfo(name=name, database=db, members=members))
            return groups

    def create_group(self, group: str) -> GroupInfo:
        db = self._dbname(group)
        password = "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
        dbq = self._quote_ident(db)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SHOW DATABASES LIKE %s", (db,))
            if cur.fetchone():
                raise SqlShareError(f"グループ「{group}」は既に存在します")
            cur.execute(f"CREATE DATABASE {dbq} CHARACTER SET utf8mb4")
            cur.execute(
                f"CREATE TABLE {dbq}.{META_TABLE} ("
                " k VARCHAR(191) PRIMARY KEY, v TEXT) CHARACTER SET utf8mb4")
            cur.execute(
                f"INSERT INTO {dbq}.{META_TABLE} (k, v) VALUES ('password', %s)",
                (password,))
        return GroupInfo(name=group, database=db)

    def delete_group(self, group: str) -> None:
        db = self._dbname(group)
        with self._connect() as conn, conn.cursor() as cur:
            for ip in self._members(cur, db):
                self._drop_user(cur, db, ip)
            cur.execute(f"DROP DATABASE IF EXISTS {self._quote_ident(db)}")

    # ---------- サーバー(メンバー)操作 ----------

    def add_server(self, group: str, ip: str) -> None:
        db = self._dbname(group)
        with self._connect() as conn, conn.cursor() as cur:
            password = self._meta_get(cur, db, "password")
            if password is None:
                raise SqlShareError(f"グループ「{group}」のメタ情報が見つかりません")
            cur.execute("CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                        (db, ip, password))
            cur.execute(
                f"GRANT ALL PRIVILEGES ON {self._quote_ident(db)}.* TO %s@%s",
                (db, ip))
            cur.execute(
                f"INSERT IGNORE INTO {self._quote_ident(db)}.{META_TABLE} (k, v)"
                " VALUES (%s, '')", (f"member:{ip}",))

    def remove_server(self, group: str, ip: str) -> None:
        db = self._dbname(group)
        with self._connect() as conn, conn.cursor() as cur:
            self._drop_user(cur, db, ip)
            cur.execute(
                f"DELETE FROM {self._quote_ident(db)}.{META_TABLE} WHERE k = %s",
                (f"member:{ip}",))

    def update_member_ip(self, old_ip: str, new_ip: str) -> list[str]:
        """全グループで old_ip のメンバーシップを new_ip に付け替える。

        サーバーのIP変更に追随するためのもの。影響を受けたグループ名を返す。
        """
        changed = []
        for group in self.list_groups():
            if old_ip in group.members:
                self.remove_server(group.name, old_ip)
                self.add_server(group.name, new_ip)
                changed.append(group.name)
        return changed

    def _members(self, cur, db: str) -> list[str]:
        try:
            cur.execute(
                f"SELECT k FROM {self._quote_ident(db)}.{META_TABLE}"
                " WHERE k LIKE 'member:%'")
            return [r[0].split(":", 1)[1] for r in cur.fetchall()]
        except Exception:
            return []

    @staticmethod
    def _drop_user(cur, user: str, ip: str) -> None:
        cur.execute("DROP USER IF EXISTS %s@%s", (user, ip))

    # ---------- 接続情報 ----------

    def connection_info(self, group: str) -> dict:
        """プラグイン設定用の接続情報を返す。"""
        db = self._dbname(group)
        with self._connect() as conn, conn.cursor() as cur:
            password = self._meta_get(cur, db, "password")
            members = self._members(cur, db)
        return {
            "host": self.cfg.host,
            "port": self.cfg.port,
            "database": db,
            "user": db,           # ユーザー名=DB名
            "password": password or "(不明)",
            "members": members,
        }
