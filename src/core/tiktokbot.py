import json
import os
import re
import sys
import time
from typing import Optional

import ffmpeg

from utils.custom_exceptions import AccountPrivate, CountryBlacklisted, \
    LiveNotFound, UserNotLiveException, IPBlockedByWAF, LiveRestriction
from utils.enums import Mode, Error, StatusCode, TimeOut
from http_utils.http_client import HttpClient


class TikTok:
    def __init__(self, httpclient: HttpClient, output: str, mode: Mode, logger, cookies: dict,
                 url: Optional[str] = None, user: Optional[str] = None, room_id: Optional[str] = None,
                 use_ffmpeg: Optional[bool] = None, duration: Optional[int] = None, convert: bool = False):
        self.url = url
        self.user = user
        self.room_id = room_id

        self.httpclient = httpclient.req
        self.mode = mode
        self.cookies = cookies

        self.use_ffmpeg = use_ffmpeg
        self.duration = duration
        self.convert = convert

        self.output = output
        self.logger = logger

        if self.is_country_blacklisted():
            if room_id is None:
                raise CountryBlacklisted(Error.BLACKLIST_ERROR)
            if mode == Mode.AUTOMATIC:
                raise ValueError(Error.AUTOMATIC_MODE_ERROR)

        if self.url:
            self.get_room_and_user_from_url()
        if not self.user:
            self.user = self.get_user_from_room_id()
        if not self.room_id:
            self.room_id = self.get_room_id_from_user()

        self.logger.info(f"USERNAME: {self.user}")
        if not self.room_id:
            self.logger.info(f"ROOM_ID: {Error.USER_NEVER_BEEN_LIVE}")
        else:
            self.logger.info(f"ROOM_ID:  {self.room_id}")

        self.httpclient = HttpClient(self.logger, cookies=self.cookies, proxy=None).req

    def run(self):
        if self.mode == Mode.MANUAL:
            if not self.room_id:
                raise UserNotLiveException(Error.USER_NEVER_BEEN_LIVE)
            if not self.is_user_in_live():
                raise UserNotLiveException(Error.USER_NOT_CURRENTLY_LIVE)
            self.start_recording()
        elif self.mode == Mode.AUTOMATIC:
            self.run_automatic_mode()

    def run_automatic_mode(self):
        while True:
            try:
                self.room_id = self.get_room_id_from_user()
                if not self.room_id:
                    raise UserNotLiveException(Error.USER_NEVER_BEEN_LIVE)
                if not self.is_user_in_live():
                    raise UserNotLiveException(Error.USER_NOT_CURRENTLY_LIVE)
                self.start_recording()
            except UserNotLiveException as ex:
                self.logger.info(ex)
                self.logger.info(f"Waiting {TimeOut.AUTOMATIC_MODE} minutes before recheck\n")
                time.sleep(TimeOut.AUTOMATIC_MODE * TimeOut.ONE_MINUTE)
            except ConnectionAbortedError:
                self.logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)
            except Exception as ex:
                self.logger.error(ex)

    def convertion_mp4(self, file: str):
        try:
            self.logger.info(f"Converting {file} to MP4 format...")
            ffmpeg.input(file).output(file.replace('_flv.mp4', '.mp4'), y='-y').run(quiet=True)
            os.remove(file)
            self.logger.info(f"Finished converting {file}")
        except FileNotFoundError:
            self.logger.error("FFmpeg is not installed. -> pip install ffmpeg-python")

    def start_recording(self):
        live_url = self.get_live_url()
        if not live_url:
            raise LiveNotFound(Error.URL_NOT_FOUND)

        current_date = time.strftime("%Y.%m.%d_%H-%M-%S", time.localtime())
        output_path = self.prepare_output_path(current_date)

        self.logger.info(f"Start recording for {self.duration} seconds" if self.duration else "Started recording...")

        try:
            if self.use_ffmpeg:
                self.record_with_ffmpeg(live_url, output_path)
            else:
                self.record_with_stream(live_url, output_path)
        except ffmpeg.Error as e:
            self.logger.error('FFmpeg Error:')
            self.logger.error(e.stderr.decode('utf-8'))
        except FileNotFoundError:
            self.logger.error("FFmpeg is not installed -> pip install ffmpeg-python")
            sys.exit(1)
        except KeyboardInterrupt:
            pass

        self.logger.info(f"FINISH: {output_path}\n")

        if not self.use_ffmpeg and not self.convert:
            if input("Do you want to convert it to real mp4? [Y/N] -> ").lower() == "y":
                self.convertion_mp4(output_path)
        elif self.convert:
            self.convertion_mp4(output_path)

    def prepare_output_path(self, current_date: str) -> str:
        output_dir = self.output
        if output_dir and not (output_dir.endswith('/') or output_dir.endswith('\\')):
            output_dir += "\\" if os.name == 'nt' else "/"
        return f"{output_dir if output_dir else ''}TK_{self.user}_{current_date}_flv.mp4"

    def record_with_ffmpeg(self, live_url: str, output: str):
        self.logger.info("[PRESS 'q' TO STOP RECORDING]")
        stream = ffmpeg.input(live_url)
        if self.duration:
            stream = ffmpeg.output(stream, output.replace("_flv.mp4", ".mp4"), c='copy', t=self.duration)
        else:
            stream = ffmpeg.output(stream, output.replace("_flv.mp4", ".mp4"), c='copy')
        ffmpeg.run(stream, quiet=True)

    def record_with_stream(self, live_url: str, output: str):
        self.logger.info("[PRESS ONLY ONCE CTRL + C TO STOP]")
        response = self.httpclient.get(live_url, stream=True)
        with open(output, "wb") as out_file:
            start_time = time.time()
            for chunk in response.iter_content(chunk_size=4096):
                out_file.write(chunk)
                if self.duration and (time.time() - start_time) >= self.duration:
                    break

    def get_live_url(self) -> str:
        url = f"https://webcast.tiktok.com/webcast/room/info/?aid=1988&room_id={self.room_id}"
        data = self.httpclient.get(url).json()

        if 'This account is private' in data:
            raise AccountPrivate

        live_url_flv = data.get('data', {}).get('stream_url', {}).get('rtmp_pull_url', None)
        if live_url_flv is None and data.get('status_code') == 4003110:
            raise LiveRestriction

        self.logger.info(f"LIVE URL: {live_url_flv}")

        return live_url_flv

    def is_user_in_live(self) -> bool:
        url = f"https://webcast.tiktok.com:443/webcast/room/check_alive/?aid=1988&region=CH&room_ids={self.room_id}&user_is_login=true"
        data = self.httpclient.get(url).json()

        return data.get('data', [{}])[0].get('alive', False)

    def get_room_and_user_from_url(self):
        response = self.httpclient.get(self.url, allow_redirects=False)
        content = response.text

        if response.status_code == StatusCode.REDIRECT:
            raise CountryBlacklisted('Redirect')

        if response.status_code == StatusCode.MOVED:
            matches = re.findall("com/@(.*?)/live", content)
            if not matches:
                raise LiveNotFound(Error.LIVE_NOT_FOUND)
            self.user = matches[0]

        match = re.match(r"https?://(?:www\.)?tiktok\.com/@([^/]+)/live", self.url)
        if match:
            self.user = match.group(1)

        self.room_id = self.get_room_id_from_user()

    def get_room_id_from_user(self) -> str:
        content = self.httpclient.get(f'https://www.tiktok.com/@{self.user}/live').text
        if 'Please wait...' in content:
            raise IPBlockedByWAF

        match = re.search(r'<script id="SIGI_STATE" type="application/json">(.*?)</script>', content, re.DOTALL)
        if not match:
            raise ValueError("Error extracting roomId")

        data = json.loads(match.group(1))

        if 'LiveRoom' not in data and 'CurrentRoom' in data:
            return ""

        room_id = data.get('LiveRoom', {}).get('liveRoomUserInfo', {}).get('user', {}).get('roomId', None)
        if room_id is None:
            raise ValueError("RoomId not found.")

        return room_id

    def get_user_from_room_id(self) -> str:
        url = f"https://www.tiktok.com/api/live/detail/?aid=1988&roomID={self.room_id}"
        data = self.httpclient.get(url).json()

        unique_id = data.get('LiveRoomInfo', {}).get('ownerInfo', {}).get('uniqueId', None)
        if not unique_id:
            raise AttributeError(Error.USERNAME_ERROR)

        return unique_id

    def is_country_blacklisted(self) -> bool:
        response = self.httpclient.get(f"https://www.tiktok.com/@{self.user}/live", allow_redirects=False)
        return response.status_code == StatusCode.REDIRECT
