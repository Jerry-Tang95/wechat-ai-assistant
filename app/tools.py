# -*- coding: utf-8 -*-
"""实时数据工具层：检测来讯是否需要外部数据 → 拉真实数据 → 生成注入 LLM 的上下文块。
数据源全部免费无 key：Open-Meteo（天气）、本机时钟（时间）、DuckDuckGo（搜索，默认关闭）。
新增工具：注册一个 (matcher, runner) 即可，引擎自动接线。"""
import re
from datetime import datetime

import requests

_TIMEOUT = 12

# WMO 天气代码 → 中文
_WMO = {0: "晴", 1: "基本晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
        51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
        61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
        71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
        80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "阵雪",
        95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹"}

_WEEK = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ---------- 工具1：天气（Open-Meteo，免费无 key） ----------
_WEATHER_RE = re.compile(r"天气|气温|温度|下雨|降雨|下雪|weather|热不热|冷不冷")
# 城市与天气关键词之间允许夹"的/今天/明天/会"等："广州的今天天气怎么样"→广州
_CITY_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,4}?)(?:市)?的?"
    r"(?:今天|明天|后天|现在|这边|那边)?的?"
    r"(?:会|会不会)?"
    r"(?:天气|气温|温度|下雨|下雪|降雨)")
_JUNK_RE = re.compile(r"的|吗|呢|吧|啊|会|今天|明天|后天|现在|这边|那边|你们|我们")
_STOP_CITIES = {"今天", "明天", "后天", "现在", "什么", "哪个", "哪里", "你们",
                "我们", "这里", "那里", "上午", "下午", "晚上"}


def _clean_city(raw):
    """清洗捕获串 → 有效城市名；无效返回 None"""
    s = _JUNK_RE.sub("", raw or "").strip()
    if not s or not (2 <= len(s) <= 4) or s in _STOP_CITIES:
        return None
    return s


def _weather_match(content):
    return bool(_WEATHER_RE.search(content))


def _weather_run(content):
    city = None
    m = _CITY_RE.search(content)
    if m:
        city = _clean_city(m.group(1))
    if not city:
        return ("【工具提示】对方询问天气但未说明城市。请自然地询问对方想查询哪个城市的天气，"
                "不要编造任何天气数据。")
    geo = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "zh", "format": "json"},
        timeout=_TIMEOUT).json()
    hits = geo.get("results") or []
    if not hits:
        return f"【工具提示】未找到城市「{city}」的地理信息。请告诉对方暂时查不到该城市天气，不要编造。"
    loc = hits[0]
    where = loc.get("name", city)
    if loc.get("admin1") and loc["admin1"] != where:
        where = f"{loc['admin1']}·{where}"
    f = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": loc["latitude"], "longitude": loc["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,weather_code",
            "timezone": "auto", "forecast_days": 2,
        }, timeout=_TIMEOUT).json()
    cur = f.get("current", {})
    daily = f.get("daily", {})
    desc = _WMO.get(cur.get("weather_code"), "未知天况")

    def _day(i):
        try:
            code = _WMO.get(daily["weather_code"][i], "")
            return (f"{daily['temperature_2m_min'][i]}~{daily['temperature_2m_max'][i]}°C，"
                    f"{code}，降水概率{daily['precipitation_probability_max'][i]}%")
        except Exception:
            return "数据缺失"

    return (f"【实时天气数据｜{where}】当前{cur.get('temperature_2m')}°C"
            f"（体感{cur.get('apparent_temperature')}°C），{desc}，"
            f"湿度{cur.get('relative_humidity_2m')}%，风速{cur.get('wind_speed_10m')}km/h。"
            f"今天：{_day(0)}。明天：{_day(1)}。"
            f"数据时间：{datetime.now().strftime('%H:%M')}。"
            f"请基于以上数据自然回答，不要编造数据之外的天况。")


# ---------- 工具2：时间日期（本机时钟） ----------
_TIME_ASK_RE = re.compile(r"几点(了|呢|啦)?|现在时间|今天几号|今天是几号|今天日期|"
                          r"星期几|礼拜几|今天星期|什么日子|几月几号")


def _time_match(content):
    return bool(_TIME_ASK_RE.search(content))


def _time_run(content):
    now = datetime.now()
    return (f"【实时时钟】现在是{now.year}年{now.month}月{now.day}日 "
            f"{now.strftime('%H:%M')}（{_WEEK[now.weekday()]}），本地时区。"
            f"请据此回答，不要说不知道时间。")


# ---------- 工具3：联网搜索（DuckDuckGo，免费无 key，默认关闭） ----------
_SEARCH_RE = re.compile(r"搜(一)?下|搜索|查一下|帮我查|查查|最新消息|有什么新闻|"
                        r"汇率|股价|比分|开奖|今天新闻")


def _search_match(content):
    return bool(_SEARCH_RE.search(content))


def _search_run(content):
    try:
        from ddgs import DDGS
    except ImportError:
        return None  # 未安装 ddgs 则静默跳过，走普通回复
    query = re.sub(r"帮我|搜(一)?下|搜索|查一下|查查|一下|好吗|吧|？|\?", " ", content).strip()
    query = query or content
    try:
        with DDGS() as dd:
            hits = list(dd.text(query, max_results=4, region="cn-zh"))[:4]
    except Exception:
        return None
    if not hits:
        return None
    lines = [f"{i+1}. {h.get('title','')}：{h.get('body','')[:120]}"
             for i, h in enumerate(hits)]
    return ("【实时搜索结果】\n" + "\n".join(lines) +
            "\n请综合以上结果自然回答；若结果与问题无关请如实说明，不要编造。")


# name -> (matcher, runner)
_TOOLS = [
    ("weather", _weather_match, _weather_run),
    ("clock", _time_match, _time_run),
    ("web_search", _search_match, _search_run),
]

DEFAULT_DISABLED = {"web_search"}   # 默认关闭的工具（config.tools.<name>.enabled 控制）


def match_tool(content):
    """返回命中的工具名，无则 None"""
    for name, matcher, _ in _TOOLS:
        try:
            if matcher(content):
                return name
        except Exception:
            continue
    return None


def run_tool(name, content):
    """执行工具，返回注入 LLM 的上下文文本；失败/无数据返回 None"""
    for n, _, runner in _TOOLS:
        if n == name:
            return runner(content)
    return None
