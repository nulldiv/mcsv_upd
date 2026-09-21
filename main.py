from __future__ import annotations

import hashlib
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import tempfile

import requests
import yaml


CONFIG_PATH = Path(__file__).with_name("config.yaml")

VERSIONS_API = "https://fill.papermc.io/v3/projects/paper/versions"

RELEASE_VERSION_PATTERN = re.compile(
    r"^\d+\.\d+(?:\.\d+)?$"
)


@dataclass
class Config:
    paper_path: Path
    log_path: Path
    user_agent: str
    current_version: str | None
    current_build: int | None


def load_config() -> Config:
    """YAMLから設定を読み込む"""

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    required_keys = [
        "paper_path",
        "log_path",
        "user_agent",
    ]

    for key in required_keys:
        if key not in data:
            raise ValueError(
                f"config.yaml に必須項目 '{key}' がありません"
            )

    current_version = data.get("current_version")
    current_build = data.get("current_build")

    if current_version is not None:
        current_version = str(current_version)

    if current_build is not None:
        current_build = int(current_build)

    return Config(
        paper_path=Path(data["paper_path"]),
        log_path=Path(data["log_path"]),
        user_agent=data["user_agent"],
        current_version=current_version,
        current_build=current_build,
    )


def setup_logger(log_path: Path) -> logging.Logger:
    """ロガーを設定する"""

    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("paper_updater")
    logger.setLevel(logging.INFO)

    handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def save_current_version_build(
    version: str,
    build: int,
) -> None:
    """
    config.yamlのcurrent_version/current_buildを更新する。

    一時ファイルに書き出してからreplaceすることで、
    書き込み途中でファイルが壊れる可能性を減らす。
    """

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    data["current_version"] = version
    data["current_build"] = build

    # config.yamlと同じディレクトリに一時ファイルを作成
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=CONFIG_PATH.parent,
        prefix=CONFIG_PATH.name + ".",
        suffix=".tmp",
        delete=False,
    ) as f:
        temp_path = Path(f.name)

        yaml.safe_dump(
            data,
            f,
            allow_unicode=True,
            sort_keys=False,
        )

    temp_path.replace(CONFIG_PATH)


def get_latest_version_build(
    session: requests.Session,
) -> tuple[str, int]:
    """最新のリリース版と最新ビルド番号を取得する"""

    response = session.get(
        VERSIONS_API,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()

    candidates: list[tuple[tuple[int, ...], str, int]] = []

    for version_info in data.get("versions", []):
        version_id = str(version_info["version"]["id"])

        # 正式リリース版のみ対象
        if not RELEASE_VERSION_PATTERN.match(version_id):
            continue

        builds = version_info.get("builds", [])

        if not builds:
            continue

        valid_builds = [
            int(build)
            for build in builds
            if isinstance(build, int)
        ]

        if not valid_builds:
            continue

        latest_build = max(valid_builds)

        version_tuple = tuple(
            int(x) for x in version_id.split(".")
        )

        candidates.append(
            (
                version_tuple,
                version_id,
                latest_build,
            )
        )

    if not candidates:
        raise RuntimeError(
            "正式リリース版のPaperが見つかりません"
        )

    _, latest_version, latest_build = max(
        candidates,
        key=lambda x: x[0],
    )

    return latest_version, latest_build


def get_build_info(
    session: requests.Session,
    version: str,
    build: int,
) -> dict:
    """指定バージョン・ビルドのダウンロード情報を取得する"""

    url = (
        f"https://fill.papermc.io/v3/projects/paper/"
        f"versions/{version}/builds/{build}"
    )

    response = session.get(
        url,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict):
        raise RuntimeError(
            "Build APIのレスポンスが不正です"
        )

    if data.get("id") != build:
        raise RuntimeError(
            f"ビルド番号が一致しません: "
            f"requested={build}, actual={data.get('id')}"
        )

    if data.get("channel") != "STABLE":
        raise RuntimeError(
            f"STABLEではないためダウンロードを中止します: "
            f"{data.get('channel')}"
        )

    downloads = data.get("downloads", {})
    server = downloads.get("server:default", {})

    download_url = server.get("url")
    sha256 = server.get("checksums", {}).get("sha256")

    if not download_url:
        raise RuntimeError(
            "ダウンロードURLが取得できません"
        )

    if not sha256:
        raise RuntimeError(
            "SHA-256が取得できません"
        )

    return {
        "url": download_url,
        "sha256": sha256,
    }


def download_paper(
    session: requests.Session,
    config: Config,
    download_url: str,
    expected_sha256: str,
    logger: logging.Logger,
) -> None:
    """PaperをダウンロードしてSHA-256検証後に置き換える"""

    paper_path = config.paper_path
    temp_path = paper_path.with_suffix(
        paper_path.suffix + ".download"
    )

    paper_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.info(
        "Paperをダウンロードします: %s",
        download_url,
    )

    sha256 = hashlib.sha256()

    try:
        with session.get(
            download_url,
            stream=True,
            timeout=60,
        ) as response:

            response.raise_for_status()

            with temp_path.open("wb") as f:
                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if not chunk:
                        continue

                    f.write(chunk)
                    sha256.update(chunk)

        actual_sha256 = sha256.hexdigest()

        logger.info(
            "SHA-256: %s",
            actual_sha256,
        )

        if actual_sha256.lower() != expected_sha256.lower():
            raise RuntimeError(
                "SHA-256が一致しません"
            )

        # 検証成功後に本番JARを置き換える
        temp_path.replace(paper_path)

        logger.info(
            "Paperの更新に成功しました: %s",
            paper_path,
        )

    except Exception:
        if temp_path.exists():
            temp_path.unlink()

        raise


def main() -> int:
    # 設定読み込み
    try:
        config = load_config()
    except Exception as e:
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )
        logging.exception(
            "設定ファイルの読み込みに失敗しました: %s",
            e,
        )
        return 1

    logger = setup_logger(config.log_path)

    try:
        logger.info("Paper Auto Updaterを開始します")

        logger.info(
            "現在のバージョン: %s build %s",
            config.current_version,
            config.current_build,
        )

        session = requests.Session()

        session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": "application/json",
            }
        )

        # 最新版・最新ビルドを取得
        latest_version, latest_build = (
            get_latest_version_build(session)
        )

        logger.info(
            "最新バージョン: %s build %s",
            latest_version,
            latest_build,
        )

        # 現在と最新が完全一致していれば終了
        if (
            config.current_version == latest_version
            and config.current_build == latest_build
        ):
            logger.info(
                "すでに最新バージョンです。"
            )
            return 0

        logger.info(
            "更新が必要です: %s build %s -> %s build %s",
            config.current_version,
            config.current_build,
            latest_version,
            latest_build,
        )

        # Build詳細を取得
        build_info = get_build_info(
            session,
            latest_version,
            latest_build,
        )

        # ダウンロード・SHA-256検証
        download_paper(
            session,
            config,
            build_info["url"],
            build_info["sha256"],
            logger,
        )

        # ダウンロード成功後のみYAMLを更新
        save_current_version_build(
            latest_version,
            latest_build,
        )

        logger.info(
            "config.yamlを更新しました: "
            "%s build %s",
            latest_version,
            latest_build,
        )

        logger.info(
            "Paper Auto Updaterが正常終了しました"
        )

        return 0

    except requests.RequestException:
        logger.exception(
            "HTTP通信でエラーが発生しました"
        )
        return 1

    except Exception:
        logger.exception(
            "予期しないエラーが発生しました"
        )
        return 1

    finally:
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())