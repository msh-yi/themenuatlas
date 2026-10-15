"""Hosts that aren't a restaurant's own website: news, guides, directories, booking and
social sites, and hosting platforms. Shared by scripts/find_websites.py (rejecting search
results) and the data-quality checks (flagging websites already in the database)."""

import re

NOT_OFFICIAL = re.compile(
    r"(?i)(tripadvisor|yelp|google\.|facebook|instagram|tiktok|twitter|(^|\.)x\.com$|youtube|linkedin|"
    r"michelin|theworlds50best|the50\.com|opentable|resy|exploretock|sevenrooms|thefork|quandoo|"
    r"tablecheck|tabelog|gurunavi|retty|dianping|meituan|openrice|zomato|timeout|eater\.com|"
    r"infatuation|nytimes|wikipedia|wikidata|foursquare|ubereats|deliveroo|doordash|grubhub|"
    r"(^|\.)booking\.com$|expedia|(^|\.)hotels\.com$|agoda|(^|\.)trip\.com$|gaultmillau|falstaff|restaurantguru|"
    r"menupix|sluurpy|wanderlog|(^|\.)lafourchette\.|cntraveler|forbes|condenast|guide\.|reddit|pinterest|"
    r"evendo|elitetraveler|foodle|usarestaurants|delectable|wikirank|reportergourmet|finedininglovers|"
    r"myartguides|scoop\.com|restaurantji|economictimes|livejapan|leadingrestaurants|wikiwand|"
    r"news|magazine|journal|times|washingtonpost|nypost|guardian|telegraph|travel|tourism|visit|blog|review|"
    r"directory|listing|menu(s)?\.(com|info)|yellowpages|pagesjaunes|happycow|gayot|(^|\.)bonappetit\.com$|"
    r"robbreport|cntraveller|lonelyplanet|fodors|frommers|culinarybackstreets|viamichelin|valgardena\.it|"
    r"amazon|ebay|(^|\.)apple\.com$|(^|\.)wix\.com$|twitch|threads\.net|weibo|xiaohongshu|naver\.com|"
    r"kakao|line\.me|hotpepper|ikyu|tablelog|chope|eatigo|quandoo|bookatable|(^|\.)dish\.co$)"
)
# Hosting, blog and social platforms, where a name match on the subdomain proves nothing.
PLATFORM = re.compile(r"(?i)(wordpress\.com|blogspot\.|medium\.com|substack\.com|tumblr\.com|"
                      r"wixsite\.com|weebly\.com|squarespace\.com|github\.io|snapchat\.com|"
                      r"linktr\.ee|business\.site|sites\.google\.com)")


def not_official(host: str) -> bool:
    return bool(host) and bool(NOT_OFFICIAL.search(host) or PLATFORM.search(host))
