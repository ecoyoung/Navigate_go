from datetime import UTC, datetime

from app.models import ContentItem
from app.reader_cards import extractive_paragraphs, sanitize_article_text

NOW = datetime(2026, 8, 28, 10, tzinfo=UTC)

TENCENT_BODY = """
![](https://news.qq.com/rain/a/20260828A0AP0800)
- [首页](https://www.qq.com/)
- [要闻](https://news.qq.com/)
- [科技](https://news.qq.com/ch/tech)
正在浏览：8月28日融资观察：具身智能单日吸金超70亿
搜索
登录
![](https://pgdt.gtimg.cn/x?ck=d4dd7d2a55d6ad267027ef03460d19e7&sha256=8e7133c8d22e1ea02afe6d67d0ca88a2ed5c433c69478049dc05f27c10997f04)
广告
关注
分享
# 8月28日融资观察：具身智能单日吸金超70亿
2026-08-28 18:21发布于北京
8月28日融资市场具身智能赛道迎来标志性事件，禾赛科技创始团队二次创业的Sharpa首次披露累计超45亿元融资，投后估值220亿元。
李一帆团队选择以灵巧操作为核心路径，Wave灵巧手与CraftNet基座模型双线推进。
相关推荐
[黄金布局论](https://news.qq.com/omn/author/8QMc)
当前您处于未登录状态，未登录状态下腾讯广告无法为您在PC网站上提供个性化广告推荐服务。
"""


def _content(**kwargs) -> ContentItem:
    payload = {
        "id": 1,
        "source_id": 1,
        "raw_item_id": 1,
        "identity_key": "a" * 64,
        "title": "8月28日融资观察：具身智能单日吸金超70亿_腾讯新闻",
        "body": TENCENT_BODY,
        "excerpt": (
            "8月28日融资观察：具身智能单日吸金超70亿"
            "8月28日融资市场具身智能赛道迎来标志性事件，"
            "禾赛科技创始团队二次创业的Sharpa首次披露累计超45亿元融资。"
        ),
        "content_hash": "b" * 64,
        "published_at": NOW,
        "discovered_at": NOW,
        "quality": {"body_complete": True, "metadata_only": False},
    }
    payload.update(kwargs)
    item = ContentItem()
    for key, value in payload.items():
        setattr(item, key, value)
    return item


def test_sanitize_drops_tencent_nav_ads_and_markdown():
    text = sanitize_article_text(TENCENT_BODY, title="8月28日融资观察：具身智能单日吸金超70亿")
    assert "Sharpa" in text
    for noise in ("首页", "登录", "广告", "http", "ck=", "sha256", "相关推荐", "未登录"):
        assert noise not in text


def test_extractive_card_uses_lede_not_chrome():
    paragraphs = extractive_paragraphs(_content())
    blob = " ".join(paragraphs)
    assert "Sharpa" in blob
    assert "45亿" in blob
    for noise in ("首页", "登录", "广告", "http", "](", "ck=", "搜索", "网页设置"):
        assert noise not in blob


KR36_EXCERPT = (
    "图源/企业 本文约 3300 字，建议阅读 8 分钟 作者丨欧雪 编辑丨袁斯来 "
    "硬氪获悉，湖北汉鼎智能科技有限公司（以下简称“汉鼎智能”）已于近期完成数千万元战略轮融资。"
)


def test_sanitize_drops_36kr_byline_and_reading_time():
    text = sanitize_article_text(KR36_EXCERPT, title="国内无人物流车转向市占率超50%")
    assert "汉鼎智能" in text
    assert "战略轮融资" in text
    for noise in ("图源", "本文约", "建议阅读", "作者丨", "编辑丨", "欧雪", "袁斯来", "3300"):
        assert noise not in text


def test_extractive_card_keeps_lede_not_36kr_byline():
    paragraphs = extractive_paragraphs(
        _content(
            title="国内无人物流车转向市占率超50%，对标博世采埃孚，获数千万元战略轮融资丨36氪首发",
            excerpt=KR36_EXCERPT,
            body=KR36_EXCERPT,
        )
    )
    blob = " ".join(paragraphs)
    assert "汉鼎" in blob
    assert "融资" in blob
    for noise in ("图源", "本文约", "建议阅读", "作者丨", "编辑丨", "欧雪", "袁斯来"):
        assert noise not in blob


def test_sanitize_drops_pipe_bylines_and_empty_reading_time():
    text = sanitize_article_text(
        "作者｜黄楠 编辑｜袁斯来 36氪获悉，9月2日，前字节跳动强化学习专家孙鹏加入星尘智能。"
    )
    assert "36氪获悉" in text
    assert "星尘智能" in text
    assert "黄楠" not in text
    assert "袁斯来" not in text

    empty = sanitize_article_text(
        "本文共字，预计阅读时间。导语 当下全球人工智能竞赛，舆论焦点始终聚焦于中美双雄博弈。"
    )
    assert "人工智能竞赛" in empty
    assert "本文共字" not in empty
    assert "预计阅读时间" not in empty
    assert not empty.startswith("导语")


def test_byline_strip_keeps_author_opinion_and_design_copy():
    opinion = sanitize_article_text("作者认为，具身智能融资将继续向头部集中。")
    assert "作者认为" in opinion
    design = sanitize_article_text("新机设计：简约风格，适合实验室场景。")
    assert "简约风格" in design
    assert "实验室场景" in design
