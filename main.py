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


# ============================================================
# Configuration
# ============================================================

CONFIG_PATH = Path(__file__).with_name("config.yaml")

VERSIONS_API = "https://fill.papermc.io/v3/projects/paper/versions"

# 正式リリースのみを対象とする
#
# 対象:
#   1.21
#   1.21.8
#   1.21.11
#
# 除外:
#   1.21-pre1
#   1.21.1-rc1
#   24w14a
#   その他snapshot等
RELEASE_VERSION_PATTERN = re.compile(
    r"^\d+\.\d+(?:\.\d+)?$"
)

USER_AGENT='PaperAutoUpdater/1.0 ([mcsv_upd](https://github.com/nulldiv/mcsv_upd))'

HTTP_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120

DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MiB

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


# ============================================================
# Data classes
# ============================================================

@dataclass(frozen=True)
class Config:
    """
    config.yamlから読み込んだ設定。
    """

    paper_path: Path
    log_path: Path

    last_downloaded_version: str | None
    last_downloaded_build: int | None


# ============================================================
# Configuration
# ============================================================

def load_config() -> Config:
    """YAMLから設定を読み込む"""

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config file not found: {CONFIG_PATH}"
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a YAML mapping.")

    # 必須設定
    required_keys = [
        "paper_path",
        "log_path",
    ]

    for key in required_keys:
        if key not in data:
            raise ValueError(
                f"Missing required config key: {key}"
            )

    # last_downloaded_* は初回実行時にはnullでもよい
    version = data.get("last_downloaded_version")
    build = data.get("last_downloaded_build")

    if version is not None:
        version = str(version)

    if build is not None:
        try:
            build = int(build)
        except (TypeError, ValueError) as e:
            raise ValueError(
                "last_downloaded_build must be an integer or null."
            ) from e

    return Config(
        paper_path=Path(data["paper_path"]),
        log_path=Path(data["log_path"]),
        last_downloaded_version=version,
        last_downloaded_build=build,
    )


# ============================================================
# Logging
# ============================================================

def setup_logger(log_path: Path) -> logging.Logger:
    """ロガーを設定する"""

    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("paper_updater")
    logger.setLevel(logging.INFO)

    # main()が複数回呼ばれてもhandlerが重複しないようにする
    logger.handlers.clear()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.DEBUG)
    logger.addHandler(stream_handler)

    return logger


# ============================================================
# Paper API
# ============================================================

def parse_version(version: str) -> tuple[int, ...]:
    """
    バージョン文字列を比較用tupleに変換する。

    例:
        1.21     -> (1, 21)
        1.21.8   -> (1, 21, 8)
        1.21.11  -> (1, 21, 11)
    """

    return tuple(
        int(part)
        for part in version.split(".")
    )


# ============================================================
# YAML state
# ============================================================

def save_last_downloaded(
    version: str,
    build: int,
    logger: logging.Logger,
) -> None:
    """
    config.yamlのlast_downloaded_*を更新する。

    元のconfig.yamlを直接書き換えず、
    一時ファイルを書いてからreplaceする。
    """

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(
            "config.yaml must contain a YAML mapping."
        )

    data["last_downloaded_version"] = version
    data["last_downloaded_build"] = build

    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=CONFIG_PATH.parent,
            prefix=f".{CONFIG_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:

            temp_path = Path(temp_file.name)

            yaml.safe_dump(
                data,
                temp_file,
                allow_unicode=True,
                sort_keys=False,
            )

            temp_file.flush()

        temp_path.replace(CONFIG_PATH)

        temp_path = None

        logger.info(
            "Updated config state: "
            "last_downloaded_version=%s "
            "last_downloaded_build=%d",
            version,
            build,
        )

    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(
                    missing_ok=True
                )
            except OSError:
                logger.warning(
                    "Failed to remove temporary config file: %s",
                    temp_path,
                    exc_info=True,
                )



def get_latest_version_build(
    session: requests.Session,
    logger: logging.Logger,
) -> tuple[str, int]:
    """最新のリリース版と最新ビルド番号を取得する"""

    logger.info(
        "Requesting Paper versions API: %s",
        VERSIONS_API,
    )

    response = session.get(
        VERSIONS_API,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict):
        raise RuntimeError(
            "Unexpected response format from versions API."
        )

    versions = data.get("versions")

    if not isinstance(versions, list):
        raise RuntimeError(
            "Missing or invalid 'versions' in API response."
        )

    candidates: list[tuple[tuple[int, ...], str, int]] = []

    for version_data in versions:
        if not isinstance(version_data, dict):
            continue

        version_info = version_data.get("version")

        if not isinstance(version_info, dict):
            continue

        version = version_info.get("id")

        if not isinstance(version, str):
            continue

        # 正式リリース以外を除外
        if not RELEASE_VERSION_PATTERN.match(version):
            continue

        builds = version_data.get("builds")

        if not isinstance(builds, list):
            continue

        valid_builds: list[int] = []

        for build in builds:
            try:
                build_number = int(build)
            except (TypeError, ValueError):
                continue

            valid_builds.append(build_number)

        if not valid_builds:
            continue

        latest_build = max(valid_builds)

        candidates.append(
            (
                parse_version(version),
                version,
                latest_build,
            )
        )

    if not candidates:
        raise RuntimeError(
            "No valid Paper release versions were found."
        )

    _, latest_version, latest_build = max(
        candidates,
        key=lambda item: item[0],
    )

    logger.info(
        "Latest Paper release: version=%s build=%d",
        latest_version,
        latest_build,
    )

    return latest_version, latest_build


def get_build_info(
    session: requests.Session,
    logger: logging.Logger,
    version: str,
    build: int,
) -> tuple[str, str]:
    """
    指定されたversion/buildの詳細情報を取得する。

    戻り値:
        (download_url, expected_sha256)
    """

    url = (
        VERSIONS_API +
        f"/{version}/builds/{build}"
    )

    logger.info(
        "Requesting build information: %s",
        url,
    )

    response = session.get(
        url,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict):
        raise RuntimeError(
            "Unexpected response format from build API."
        )

    api_build = data.get("id")

    try:
        api_build = int(api_build)
    except (TypeError, ValueError) as e:
        raise RuntimeError(
            "Build API returned an invalid build id."
        ) from e

    if api_build != build:
        raise RuntimeError(
            f"Build ID mismatch: requested={build}, "
            f"received={api_build}"
        )

    downloads = data.get("downloads")

    if not isinstance(downloads, dict):
        raise RuntimeError(
            "Missing 'downloads' in build API response."
        )

    server_download = downloads.get("server:default")

    if not isinstance(server_download, dict):
        raise RuntimeError(
            "Missing 'downloads.server:default'."
        )

    download_url = server_download.get("url")

    if not isinstance(download_url, str) or not download_url:
        raise RuntimeError(
            "Missing download URL."
        )

    checksums = server_download.get("checksums")

    if not isinstance(checksums, dict):
        raise RuntimeError(
            "Missing checksums."
        )

    expected_sha256 = checksums.get("sha256")

    if (
        not isinstance(expected_sha256, str)
        or not expected_sha256
    ):
        raise RuntimeError(
            "Missing SHA-256 checksum."
        )

    logger.info(
        "Build information received: version=%s build=%d",
        version,
        build,
    )

    return download_url, expected_sha256


# ============================================================
# Download / verification
# ============================================================

def download_and_verify(
    session: requests.Session,
    config: Config,
    download_url: str,
    expected_sha256: str,
    logger: logging.Logger,
) -> None:
    """
    JARを一時ファイルへダウンロードし、
    SHA-256検証後にpaper_pathへ配置する。

    検証に失敗した場合、既存のpaper_pathは変更しない。
    """

    paper_path = config.paper_path
    paper_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # paper_pathと同じディレクトリに作ることで、
    # replace()時のrenameを同一filesystem内で行えるようにする。
    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{paper_path.name}.",
            suffix=".download",
            dir=paper_path.parent,
            delete=False,
        ) as temp_file:

            temp_path = Path(temp_file.name)

            logger.info(
                "Downloading Paper JAR: %s",
                download_url,
            )

            sha256 = hashlib.sha256()
            total_bytes = 0

            with session.get(
                download_url,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
            ) as response:

                response.raise_for_status()

                for chunk in response.iter_content(
                    chunk_size=DOWNLOAD_CHUNK_SIZE
                ):
                    if not chunk:
                        continue

                    temp_file.write(chunk)
                    sha256.update(chunk)

                    total_bytes += len(chunk)

            temp_file.flush()

        actual_sha256 = sha256.hexdigest()

        logger.info(
            "Download completed: %.2f MiB",
            total_bytes / 1024 / 1024,
        )

        if actual_sha256.lower() != expected_sha256.lower():
            raise RuntimeError(
                "SHA-256 verification failed: "
                f"expected={expected_sha256}, "
                f"actual={actual_sha256}"
            )

        logger.info(
            "SHA-256 verification succeeded: %s",
            actual_sha256,
        )

        # 検証済みファイルを正式なpaper_pathへ置き換える
        temp_path.replace(paper_path)

        temp_path = None

        logger.info(
            "Published update JAR: %s",
            paper_path,
        )

    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(
                    missing_ok=True
                )
            except OSError:
                logger.warning(
                    "Failed to remove temporary file: %s",
                    temp_path,
                    exc_info=True,
                )



# ============================================================
# Main
# ============================================================

def main() -> int:
    """
    Updater本体。

    戻り値:
        0 = 成功 / 更新不要
        1 = エラー
    """

    # まずconfigを読み込む
    config = load_config()

    logger = setup_logger(config.log_path)

    logger.info(
        "===== Paper Updater started ====="
    )

    try:
        logger.info(
            "Update path: %s",
            config.paper_path,
        )

        logger.info(
            "Last downloaded: version=%s build=%s",
            config.last_downloaded_version,
            config.last_downloaded_build,
        )

        session = requests.Session()

        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )

        # ----------------------------------------------------
        # 1. 最新版を取得
        # ----------------------------------------------------

        latest_version, latest_build = (
            get_latest_version_build(
                session,
                logger,
            )
        )

        # ----------------------------------------------------
        # 2. 最後にダウンロードしたものと比較
        # ----------------------------------------------------

        if (
            config.last_downloaded_version == latest_version
            and config.last_downloaded_build == latest_build
        ):
            logger.info(
                "Already up to date. "
                "No download is required."
            )

            return 0

        logger.info(
            "New Paper build detected: "
            "last_downloaded=%s/%s -> latest=%s/%d",
            config.last_downloaded_version,
            config.last_downloaded_build,
            latest_version,
            latest_build,
        )

        # ----------------------------------------------------
        # 3. Build詳細取得
        # ----------------------------------------------------

        download_url, expected_sha256 = get_build_info(
            session,
            logger,
            latest_version,
            latest_build,
        )

        # ----------------------------------------------------
        # 4. ダウンロード + SHA-256検証
        # ----------------------------------------------------

        download_and_verify(
            session,
            config,
            download_url,
            expected_sha256,
            logger,
        )

        # ----------------------------------------------------
        # 5. ダウンロード成功後に状態を更新
        # ----------------------------------------------------

        save_last_downloaded(
            latest_version,
            latest_build,
            logger,
        )

        logger.info(
            "Paper update acquisition completed successfully."
        )

        return 0

    except Exception:
        logger.error(
            "Paper updater failed.",
            exc_info=True,
        )

        return 1

    finally:
        logger.info("===== Paper Updater finished =====")
        logging.shutdown()


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # loggerの初期化前に発生したエラー用
        sys.exit(1)