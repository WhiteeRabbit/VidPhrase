import browser_cookie3
from pathlib import Path

OUTPUT_FILE = "1_cookies.txt"


def to_netscape(cookie):
    domain = cookie.domain
    include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
    path = cookie.path
    secure = "TRUE" if cookie.secure else "FALSE"
    expiry = int(cookie.expires) if cookie.expires else 0

    return f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{expiry}\t{cookie.name}\t{cookie.value}"


def get_cookies(browser):
    if browser == "chrome":
        return browser_cookie3.chrome()
    if browser == "firefox":
        return browser_cookie3.firefox()
    raise ValueError("Unknown browser")


def main():
    print("Select browser:")
    print("1 - Chrome")
    print("2 - Firefox")

    choice = input(">>> ").strip()

    browser_map = {
        "1": "chrome",
        "2": "firefox"
    }

    if choice not in browser_map:
        return

    browser = browser_map[choice]

    try:
        cookies = get_cookies(browser)
    except Exception as e:
        print(e)
        return

    lines = []

    for cookie in cookies:
        if "youtube.com" not in cookie.domain and ".youtube.com" not in cookie.domain:
            continue
        try:
            lines.append(to_netscape(cookie))
        except:
            pass

    Path(OUTPUT_FILE).write_text("\n".join(lines), encoding="utf-8")

    print(len(lines))


if __name__ == "__main__":
    main()