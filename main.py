from __future__ import annotations

import csv
import hashlib
import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests


# ============================================================
# 設定
# ============================================================

# Paper JARの保存先
PAPER_PATH = Path(r"C:\Minecraft\paper.jar")

# 現在保持しているPaperのversion/buildを記録するCSV
VERSION_CSV = Path(r"C:\Minecraft\paper-version.csv")

# ログファイル
LOG_PATH = Path(r"C:\Minecraft\paper-updater.log")

# PaperMC API
VERSIONS_API = (
    "https://fill.papermc.io/v3/projects/paper/versions"
)

# PaperMC API用User-Agent
USER_AGENT = (
    "PaperAutoUpdater/1.0 "
    "(https://example.com/paper-auto-updater)"
)


# ============================================================
# Logging
# ============================================================

def setup_logger() -> logging.Logger:
    """
    ロガーを初期化する。

    ログファイル:
        paper-updater.log

    10MBを超えた場合、
        paper-updater.log.1
        paper-updater.log.2
        ...
    とローテーションする。
    """

    LOG_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger = logging.getLogger("paper-updater")

    logger.setLevel(logging.INFO)

    # 二重初期化を防止
    if logger.handlers:
        return logger

    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )

    formatter = logging.Formatter(
        fmt=(
            "%(asctime)s "
            "[%(levelname)s] "
            "%(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler.setFormatter(formatter)

    logger.addHandler(handler)

    return logger


logger = setup_logger()


# ============================================================
# HTTP
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
})


def get_json(url: str) -> object:
    """JSON APIを取得する。"""

    logger.debug(
        "GET %s",
        url,
    )

    response = session.get(
        url,
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# Minecraft version
# ============================================================

RELEASE_VERSION_PATTERN = re.compile(
    r"^\d+\.\d+(?:\.\d+)?$"
)


def is_release_version(version: str) -> bool:
    """
    Minecraftの正式リリース版か判定する。

    例:

        1.21       -> True
        1.21.11    -> True

        25w10a     -> False
        1.21-pre1  -> False
        1.21-rc1   -> False
    """

    return bool(
        RELEASE_VERSION_PATTERN.fullmatch(version)
    )


def version_key(version: str) -> tuple[int, ...]:
    """version比較用のキーを作る。"""

    return tuple(
        int(part)
        for part in version.split(".")
    )


# ============================================================
# CSV
# ============================================================

def load_current_version() -> tuple[str, int] | None:
    """
    CSVから現在保持しているversion/buildを読み込む。

    CSV:

        version,build
        1.21.11,110

    CSVが存在しない場合はNone。
    """

    if not VERSION_CSV.is_file():
        logger.info(
            "Version CSV does not exist: %s",
            VERSION_CSV,
        )

        return None

    try:

        with VERSION_CSV.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as f:

            reader = csv.DictReader(f)

            row = next(reader, None)

            if row is None:
                logger.warning(
                    "Version CSV is empty: %s",
                    VERSION_CSV,
                )

                return None

            version = row.get("version")
            build = row.get("build")

            if not version or not build:
                logger.warning(
                    "Version CSV does not contain "
                    "valid version/build information: %s",
                    VERSION_CSV,
                )

                return None

            current = (
                version,
                int(build),
            )

            logger.info(
                "Current Paper: %s build %d",
                current[0],
                current[1],
            )

            return current

    except (
        OSError,
        ValueError,
        UnicodeError,
    ):
        logger.exception(
            "Failed to read version CSV: %s",
            VERSION_CSV,
        )

        return None


def save_current_version(
    version: str,
    build: int,
) -> None:
    """
    version/buildをCSVへ保存する。

    ダウンロード成功後にのみ呼び出す。
    """

    VERSION_CSV.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_path = VERSION_CSV.with_suffix(
        VERSION_CSV.suffix + ".tmp"
    )

    try:

        with temp_path.open(
            "w",
            encoding="utf-8",
            newline="",
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "version",
                    "build",
                ],
            )

            writer.writeheader()

            writer.writerow({
                "version": version,
                "build": build,
            })

        temp_path.replace(VERSION_CSV)

        logger.info(
            "Version information updated: "
            "%s build %d",
            version,
            build,
        )

    except OSError:

        logger.exception(
            "Failed to save version CSV: %s",
            VERSION_CSV,
        )

        temp_path.unlink(
            missing_ok=True,
        )

        raise


# ============================================================
# 最新version/buildの取得
# ============================================================

def get_latest_version_build() -> tuple[str, int]:
    """
    /versions APIから、
    最新の正式リリース版と最新build番号を取得する。
    """

    logger.info(
        "Checking latest Paper version..."
    )

    data = get_json(VERSIONS_API)

    if not isinstance(data, dict):
        raise RuntimeError(
            "API responseがJSON objectではありません。"
        )

    versions = data.get("versions")

    if not isinstance(versions, list):
        raise RuntimeError(
            "API responseのversionsが"
            "想定した形式ではありません。"
        )

    candidates: list[tuple[str, int]] = []

    for entry in versions:

        if not isinstance(entry, dict):
            continue

        version_info = entry.get("version")
        builds = entry.get("builds")

        if not isinstance(version_info, dict):
            continue

        if not isinstance(builds, list):
            continue

        version_id = version_info.get("id")

        if not isinstance(version_id, str):
            continue

        # snapshot / pre-release / RC等を除外
        if not is_release_version(version_id):
            continue

        # build番号だけを抽出
        valid_builds = [
            build
            for build in builds
            if isinstance(build, int)
            and not isinstance(build, bool)
        ]

        if not valid_builds:
            continue

        latest_build = max(valid_builds)

        candidates.append(
            (
                version_id,
                latest_build,
            )
        )

    if not candidates:
        raise RuntimeError(
            "正式リリース版のPaperが見つかりません。"
        )

    # Minecraft versionが最も新しいものを取得
    candidates.sort(
        key=lambda item: version_key(item[0]),
        reverse=True,
    )

    latest = candidates[0]

    logger.info(
        "Latest Paper: %s build %d",
        latest[0],
        latest[1],
    )

    return latest


# ============================================================
# build詳細情報
# ============================================================

def get_build_info(
    version: str,
    build: int,
) -> dict:
    """
    指定されたversion/buildの詳細情報を取得する。

    /versions/{version}/builds/{build}
    を使用するため、全build一覧を取得しない。
    """

    url = (
        f"{VERSIONS_API}/"
        f"{version}/builds/{build}"
    )

    logger.info(
        "Getting build information: "
        "%s build %d",
        version,
        build,
    )

    data = get_json(url)

    if not isinstance(data, dict):
        raise RuntimeError(
            "build API responseが"
            "想定した形式ではありません。"
        )

    if data.get("id") != build:
        raise RuntimeError(
            "APIから返されたbuild IDが"
            "要求したものと異なります。"
        )

    # STABLE以外は使用しない
    if data.get("channel") != "STABLE":
        raise RuntimeError(
            f"{version} build {build} は"
            f"STABLEではありません。"
        )

    downloads = data.get("downloads")

    if not isinstance(downloads, dict):
        raise RuntimeError(
            "build responseにdownloadsがありません。"
        )

    server = downloads.get("server:default")

    if not isinstance(server, dict):
        raise RuntimeError(
            "server:default downloadがありません。"
        )

    download_url = server.get("url")

    if not isinstance(download_url, str):
        raise RuntimeError(
            "Paperのdownload URLを取得できません。"
        )

    checksums = server.get("checksums")

    sha256 = None

    if isinstance(checksums, dict):
        sha256 = checksums.get("sha256")

    logger.info(
        "Build information obtained: "
        "%s build %d",
        version,
        build,
    )

    if sha256:
        logger.info(
            "SHA-256 checksum is available."
        )
    else:
        logger.warning(
            "SHA-256 checksum is not available."
        )

    return {
        "version": version,
        "build": build,
        "download_url": download_url,
        "sha256": sha256,
    }


# ============================================================
# Paperダウンロード
# ============================================================

def download_paper(info: dict) -> None:
    """
    Paper JARをダウンロードする。

    ダウンロード後にSHA-256を検証し、
    検証成功後に正式な保存先へ移動する。
    """

    PAPER_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_path = PAPER_PATH.with_suffix(
        PAPER_PATH.suffix + ".download"
    )

    version = info["version"]
    build = info["build"]

    logger.info(
        "Starting Paper download: "
        "%s build %d",
        version,
        build,
    )

    try:

        with session.get(
            info["download_url"],
            stream=True,
            timeout=60,
        ) as response:

            response.raise_for_status()

            with temp_path.open("wb") as f:

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if chunk:
                        f.write(chunk)

        logger.info(
            "Paper download completed."
        )

        # ----------------------------------------------------
        # SHA-256検証
        # ----------------------------------------------------

        expected_sha256 = info.get("sha256")

        if expected_sha256:

            logger.info(
                "Verifying SHA-256 checksum..."
            )

            sha256 = hashlib.sha256()

            with temp_path.open("rb") as f:

                while True:

                    chunk = f.read(1024 * 1024)

                    if not chunk:
                        break

                    sha256.update(chunk)

            actual_sha256 = sha256.hexdigest()

            if actual_sha256.lower() != (
                expected_sha256.lower()
            ):

                logger.error(
                    "SHA-256 checksum verification failed."
                )

                logger.error(
                    "Expected: %s",
                    expected_sha256,
                )

                logger.error(
                    "Actual:   %s",
                    actual_sha256,
                )

                temp_path.unlink(
                    missing_ok=True,
                )

                raise RuntimeError(
                    "SHA-256 checksum verification failed."
                )

            logger.info(
                "SHA-256 checksum verified successfully."
            )

        # ----------------------------------------------------
        # ダウンロード成功
        # ----------------------------------------------------

        temp_path.replace(PAPER_PATH)

        logger.info(
            "Paper updated successfully: %s",
            PAPER_PATH,
        )

    except Exception:

        temp_path.unlink(
            missing_ok=True,
        )

        logger.exception(
            "Paper download failed: "
            "%s build %d",
            version,
            build,
        )

        raise


# ============================================================
# メイン
# ============================================================

def main() -> int:

    logger.info(
        "========== Paper updater started =========="
    )

    try:

        # ----------------------------------------------------
        # 1.
        # CSVから現在のversion/buildを取得
        #
        # JARは展開しない
        # ----------------------------------------------------

        current = load_current_version()

        if current is None:

            logger.info(
                "Current Paper version is unknown."
            )

        else:

            logger.info(
                "Current Paper: %s build %d",
                current[0],
                current[1],
            )

        # ----------------------------------------------------
        # 2.
        # /versionsから最新version/buildを取得
        # ----------------------------------------------------

        latest_version, latest_build = (
            get_latest_version_build()
        )

        # ----------------------------------------------------
        # 3.
        # version/buildが両方一致していれば終了
        #
        # build詳細APIにもアクセスしない。
        # ----------------------------------------------------

        if current is not None:

            if (
                current[0] == latest_version
                and current[1] == latest_build
            ):

                logger.info(
                    "Paper is already up to date. "
                    "No download required."
                )

                return 0

        # ----------------------------------------------------
        # 4.
        # 更新がある場合だけbuild詳細を取得
        # ----------------------------------------------------

        if current is None:

            logger.info(
                "No current version information found. "
                "Downloading latest Paper."
            )

        else:

            logger.info(
                "Paper update detected: "
                "%s build %d -> %s build %d",
                current[0],
                current[1],
                latest_version,
                latest_build,
            )

        build_info = get_build_info(
            latest_version,
            latest_build,
        )

        # ----------------------------------------------------
        # 5.
        # Paperをダウンロード
        # ----------------------------------------------------

        download_paper(build_info)

        # ----------------------------------------------------
        # 6.
        # ダウンロード成功後にCSVを更新
        # ----------------------------------------------------

        save_current_version(
            latest_version,
            latest_build,
        )

        logger.info(
            "========== Paper updater completed "
            "successfully =========="
        )

        return 0

    except requests.RequestException:

        logger.exception(
            "HTTP request failed."
        )

        logger.error(
            "========== Paper updater failed =========="
        )

        return 1

    except Exception:

        logger.exception(
            "Unexpected error occurred."
        )

        logger.error(
            "========== Paper updater failed =========="
        )

        return 1


if __name__ == "__main__":

    exit_code = main()

    # logging handlerを確実にflush
    logging.shutdown()

    sys.exit(exit_code)
