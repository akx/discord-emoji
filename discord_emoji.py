import argparse
import dataclasses
import json
import logging
import os
from collections.abc import Iterable
from functools import partial
from multiprocessing.pool import ThreadPool
from pathlib import Path

import httpx
import rich.logging
import rich.progress

progress: rich.progress.Progress

log = logging.getLogger("discord_emoji")


@dataclasses.dataclass(frozen=True)
class DownloadJob:
    description: str
    source_url: str
    dest_path: Path

    @property
    def done(self) -> bool:
        return self.dest_path.is_file()


def get_emoji_url(emoji_id: str, animated: bool = False) -> str:
    ext = "gif" if animated else "png"
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?v=1"


def get_sticker_url(sticker_id: str) -> str:
    return f"https://media.discordapp.net/stickers/{sticker_id}.png?size=1024"


def clean_name_for_fs(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " _-").strip()


def get_my_guilds_info(discord_api_client: httpx.Client, task: rich.progress.TaskID) -> Iterable[dict]:
    guilds_resp = discord_api_client.get("https://discord.com/api/v10/users/@me/guilds")
    guilds_resp.raise_for_status()
    guilds = guilds_resp.json()
    for guild in progress.track(
        guilds,
        description="Fetching guild info",
        task_id=task,
    ):
        guild_resp = discord_api_client.get(f"https://discord.com/api/v10/guilds/{guild['id']}")
        guild_resp.raise_for_status()
        data = guild_resp.json()
        yield guild | data


def get_guild_infos_by_ids(
    discord_api_client: httpx.Client,
    guild_ids: Iterable[str],
    task: rich.progress.TaskID,
) -> Iterable[dict]:
    for guild_id in progress.track(
        guild_ids,
        description="Fetching guild info",
        task_id=task,
    ):
        guild_resp = discord_api_client.get(f"https://discord.com/api/v10/guilds/{guild_id}")
        guild_resp.raise_for_status()
        data = guild_resp.json()
        yield data


def get_guild_infos_by_source_emoji_ids(
    discord_api_client: httpx.Client,
    source_emoji_ids: Iterable[str],
    task: rich.progress.TaskID,
) -> Iterable[dict]:
    for emoji_id in progress.track(
        source_emoji_ids,
        description="Fetching guild info",
        task_id=task,
    ):
        guild_resp = discord_api_client.get(f"https://discord.com/api/v9/emojis/{emoji_id}/source")
        guild_resp.raise_for_status()
        data = guild_resp.json()
        yield data["guild"]


def get_downloads_from_guild_info(root_path: Path, guild: dict) -> Iterable[DownloadJob]:
    guild_path = root_path / clean_name_for_fs(guild["name"])
    guild_path.mkdir(exist_ok=True, parents=True)
    (guild_path / "info.json").write_text(json.dumps(guild, sort_keys=True, indent=2))
    emoji_path = guild_path / "emojis"
    sticker_path = guild_path / "stickers"
    for emoji in guild.get("emojis", []):
        animated = emoji["animated"]
        ext = "gif" if animated else "png"
        yield DownloadJob(
            description=f"{guild["name"]}:{emoji['name']}",
            source_url=get_emoji_url(emoji["id"], animated),
            dest_path=emoji_path / f"{clean_name_for_fs(emoji['name'])}.{ext}",
        )
    for sticker in guild.get("stickers", []):
        yield DownloadJob(
            description=f"{guild["name"]}:{sticker['name']}",
            source_url=get_sticker_url(sticker["id"]),
            dest_path=sticker_path / f"{clean_name_for_fs(sticker['name'])}.png",
        )


def download_job(job: DownloadJob, *, dl_client: httpx.Client) -> tuple[DownloadJob, bool]:
    if job.done:
        return (job, True)
    job.dest_path.parent.mkdir(parents=True, exist_ok=True)

    resp = dl_client.get(job.source_url)
    if resp.status_code == httpx.codes.NOT_FOUND:
        log.warning("Not found: %s", job.description)
        return (job, False)
    resp.raise_for_status()
    job.dest_path.write_bytes(resp.content)
    return (job, True)


def main() -> None:
    global progress
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[rich.logging.RichHandler()],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--guild-id",
        dest="guild_ids",
        required=False,
        nargs="+",
        type=int,
        metavar="GID",
    )
    ap.add_argument(
        "--source-emoji-id",
        dest="source_emoji_ids",
        required=False,
        type=int,
        nargs="+",
        metavar="EID",
    )
    ap.add_argument("--my-guilds", required=False, action="store_true")
    args = ap.parse_args()
    token_from_env = os.environ.get("DISCORD_USER_TOKEN")
    if not token_from_env:
        ap.error("DISCORD_USER_TOKEN is required")

    root_path = Path("download")

    jobs = []

    with rich.progress.Progress() as progress:
        with httpx.Client(headers={"Authorization": token_from_env}) as discord_api_client:
            task = progress.add_task("Fetching guilds")
            if args.my_guilds:
                guilds = get_my_guilds_info(discord_api_client, task)
            elif args.guild_ids:
                guilds = get_guild_infos_by_ids(discord_api_client, args.guild_ids, task)
            elif args.source_emoji_ids:
                guilds = get_guild_infos_by_source_emoji_ids(
                    discord_api_client,
                    args.source_emoji_ids,
                    task,
                )
            else:
                ap.error("Don't know where to get emojis from")
            for guild in guilds:
                jobs.extend(get_downloads_from_guild_info(root_path, guild))
                progress.update(task, description=f"{len(jobs)} jobs so far...")
            progress.remove_task(task)

        log.info("%d download jobs, seeing what remains to be done...", len(jobs))

        jobs = [job for job in jobs if not job.done]

        log.info("%d jobs to do.", len(jobs))

        if jobs:
            with httpx.Client() as dl_client:
                task = progress.add_task("Downloading")
                with ThreadPool(3) as pool:
                    bound_download_job = partial(download_job, dl_client=dl_client)
                    for job, _status in progress.track(
                        pool.imap_unordered(bound_download_job, jobs),
                        task_id=task,
                        total=len(jobs),
                    ):
                        progress.update(task, description=job.description[:30].ljust(30))


if __name__ == "__main__":
    main()
