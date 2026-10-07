import json
import aiohttp
from astrbot import logger

# 偷来的key
PARAMS = "D33zyir4L/58v1qGPcIPjSee79KCzxBIBy507IYDB8EL7jEnp41aDIqpHBhowfQ6iT1Xoka8jD+0p44nRKNKUA0dv+n5RWPOO57dZLVrd+T1J/sNrTdzUhdHhoKRIgegVcXYjYu+CshdtCBe6WEJozBRlaHyLeJtGrABfMOEb4PqgI3h/uELC82S05NtewlbLZ3TOR/TIIhNV6hVTtqHDVHjkekrvEmJzT5pk1UY6r0="
ENC_SEC_KEY = "45c8bcb07e69c6b545d3045559bd300db897509b8720ee2b45a72bf2d3b216ddc77fb10daec4ca54b466f2da1ffac1e67e245fea9d842589dc402b92b262d3495b12165a721aed880bf09a0a99ff94c959d04e49085dc21c78bbbe8e3331827c0ef0035519e89f097511065643120cbc478f9c0af96400ba4649265781fc9079"


class NetEaseMusicAPI:
    """
    网易云音乐API
    """

    def __init__(self):
        self.header = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
        }
        self.headers = {"referer": "http://music.163.com"}
        self.cookies = {"appver": "2.0.2"}
        self.session = aiohttp.ClientSession()

    async def _request(
        self,
        url: str,
        data: dict = {},
        method: str = "GET",
    ):
        """统一请求接口"""
        if method.upper() == "POST":
            async with self.session.post(
                url, headers=self.header, cookies=self.cookies, data=data
            ) as response:
                if response.headers.get("Content-Type") == "application/json":
                    return await response.json()
                else:
                    return json.loads(await response.text())

        elif method.upper() == "GET":
            async with self.session.get(
                url, headers=self.headers, cookies=self.cookies
            ) as response:
                return await response.json()
        else:
            raise ValueError("不支持的请求方式")

    async def fetch_data(self, keyword: str, limit=5) -> list[dict]:
        """搜索歌曲"""
        url = "http://music.163.com/api/search/get/web"
        data = {"s": keyword, "limit": limit, "type": 1, "offset": 0}
        result = await self._request(url, data=data, method="POST")
        return [
            {
                "id": song["id"],
                "name": song["name"],
                "artists": "、".join(artist["name"] for artist in song["artists"]),
                "duration": song["duration"],
            }
            for song in result["result"]["songs"][:limit]
        ]

    async def fetch_comments(self, song_id: int):
        """获取热评"""
        url = f"https://music.163.com/weapi/v1/resource/hotcomments/R_SO_4_{song_id}?csrf_token="
        data = {
            "params": PARAMS,
            "encSecKey": ENC_SEC_KEY,
        }
        result = await self._request(url, data=data, method="POST")
        return result.get("hotComments", [])

    async def fetch_lyrics(self, song_id):
        """获取歌词"""
        url = f"https://netease-music.api.harisfox.com/lyric?id={song_id}"
        result = await self._request(url)
        return result.get("lrc", {}).get("lyric", "歌词未找到")

    async def fetch_extra(self, song_id: str | int) -> dict[str, str]:
        """
        获取额外信息
        """
        url = f"https://www.hhlqilongzhu.cn/api/dg_wyymusic.php?id={song_id}&br=7&type=json"
        result = await self._request(url)
        return {
            "title": result.get("title"),
            "author": result.get("singer"),
            "cover_url": result.get("cover"),
            "audio_url": result.get("music_url"),
        }
    async def close(self):
        await self.session.close()
class NetEaseMusicAPINodeJs:
    """
    网易云音乐API NodeJs版本（具备 SSL 容错与全自动官方降级机制）
    """
    def __init__(self, base_url: str):
        self.base_url = (base_url or "").rstrip("/")
        # 使用非严格 SSL 校验及安全超时配置，避免老旧服务器/自签名证书 SSL 握手失败崩溃
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=8)
        self.session = aiohttp.ClientSession(base_url=self.base_url, connector=connector, timeout=timeout)
        self._fallback_api = NetEaseMusicAPI()

    async def _request(self, url: str, data: dict = {}, method: str = "GET"):
        if method.upper() == "POST":
            async with self.session.post(url, data=data) as response:
                if response.headers.get("Content-Type") == "application/json":
                    return await response.json()
                else:
                    return json.loads(await response.text())
        elif method.upper() == "GET":
            async with self.session.get(url) as response:
                return await response.json()
        else:
            raise ValueError("不支持的请求方式")

    async def fetch_data(self, keyword: str, limit=5) -> list[dict]:
        """搜索歌曲（失败时自动无缝降级至网易云原生官方接口）"""
        url = "/search"
        data = {"keywords": keyword, "limit": limit, "type": 1, "offset": 0}

        try:
            result = await self._request(url, data=data, method="POST")
            songs = result.get("result", {}).get("songs", [])
            if not songs:
                raise ValueError("Node.js API 返回空歌曲列表")
            return [
                {
                    "id": song["id"],
                    "name": song["name"],
                    "artists": "、".join(artist["name"] for artist in song.get("artists", [])),
                    "duration": song.get("duration", 0),
                }
                for song in songs[:limit]
            ]
        except Exception as e:
            logger.warning(f"Node.js 音乐接口 ({self.base_url}) 请求失败: {e}，正在自动降级至网易云原生官方接口...")
            return await self._fallback_api.fetch_data(keyword=keyword, limit=limit)

    async def fetch_comments(self, song_id: int):
        """获取热评（失败时自动降级）"""
        url = "/comment/hot"
        data = {
            "id": song_id,
            "type": 0,
        }
        try:
            result = await self._request(url, data=data, method="POST")
            return result.get("hotComments", [])
        except Exception as e:
            logger.warning(f"Node.js 获取热评失败: {e}，正在降级至原生接口...")
            return await self._fallback_api.fetch_comments(song_id=song_id)

    async def fetch_lyrics(self, song_id):
        """获取歌词（失败时自动降级）"""
        url = f"/lyric?id={song_id}"
        try:
            result = await self._request(url)
            return result.get("lrc", {}).get("lyric", "歌词未找到")
        except Exception as e:
            logger.warning(f"Node.js 获取歌词失败: {e}，正在降级至原生接口...")
            return await self._fallback_api.fetch_lyrics(song_id=song_id)

    async def fetch_extra(self, song_id: str | int) -> dict[str, str]:
        """获取额外信息（失败时自动降级）"""
        url = "/song/url"
        data = {"id": song_id}
        try:
            result = await self._request(url, data=data, method="POST")
            data_list = result.get("data", [])
            if data_list:
                return {
                    "audio_url": data_list[0].get("url", "")
                }
            raise ValueError("未获取到歌曲地址")
        except Exception as e:
            logger.warning(f"Node.js 获取播放链接失败: {e}，正在降级至原生接口...")
            return await self._fallback_api.fetch_extra(song_id=song_id)

    async def close(self):
        try:
            await self.session.close()
        except Exception:
            pass
        try:
            await self._fallback_api.close()
        except Exception:
            pass
class MusicSearcher:
    """
    用于从指定音乐平台搜索歌曲信息的工具类。

    支持的平台：
    - qq: QQ 音乐
    - netease: 网易云音乐
    - kugou: 酷狗音乐
    - kuwo: 酷我音乐
    - baidu: 百度音乐
    - 1ting: 一听音乐
    - migu: 咪咕音乐
    - lizhi: 荔枝FM
    - qingting: 蜻蜓FM
    - ximalaya: 喜马拉雅
    - 5singyc: 5sing原创
    - 5singfc: 5sing翻唱
    - kg: 全民K歌

    支持的过滤条件：
    - name: 按歌曲名称搜索（默认）
    - id: 按歌曲 ID 搜索
    - url: 按音乐地址（URL）搜索
    """

    def __init__(self):
        """初始化请求 URL 和请求头"""
        self.base_url = "https://music.txqq.pro/"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }
        self.session = aiohttp.ClientSession()
    async def fetch_data(self, song_name: str, platform_type: str, limit: int = 5):
        """
        向音乐接口发送 POST 请求以获取歌曲数据

        :param song_name: 要搜索的歌曲名称
        :param platform_type: 音乐平台类型，如 'qq', 'netease' 等
        :return: 返回解析后的 JSON 数据或 None
        """
        data = {
            "input": song_name,
            "filter": "name",  # 当前固定为按名称搜索
            "type": platform_type,
            "page": 1,
        }

        try:
            async with self.session.post(
                self.base_url, data=data, headers=self.headers
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return [
                        {
                            "id": song["songid"],
                            "name": song.get("title", "未知"),
                            "artists": song.get("author", "未知"),
                            "url": song.get("url", "无"),
                            "link": song.get("link", "无"),
                            "lyrics": song.get("lrc", "无"),
                            "cover_url": song.get("pic", "无"),
                        }
                        for song in result["songs"][:limit]
                    ]
                else:
                    logger.error(f"请求失败:{response.status}")
                    return None
        except Exception as e:
            logger.error(f"请求异常: {e}")
            return None
    async def close(self):
        await self.session.close()
