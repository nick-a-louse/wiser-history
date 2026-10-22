"""The website follows my shared app standard."""
import json
import os

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")


def read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def test_page_follows_standard():
    page = read("index.html")
    assert "gomez-ui.css" in page and "gomez-ui.js" in page
    assert 'class="g-header"' in page and "g-theme" in page and 'id="updated"' in page
    # one image is both the header logo and the favicon
    assert 'class="g-app-icon" src="icon.svg' in page and 'rel="icon" href="icon.svg' in page
    assert '<meta name="theme-color" content="#b4400f">' in page
    assert "v__VERSION__" in page                       # stamped by web/Dockerfile
    assert "g-wide" not in page                         # standard 1200px column


def test_shared_files_and_icons_present():
    for f in ["gomez-ui.css", "gomez-ui.js", "icon.svg", "favicon.ico", "favicon-32.png",
              "apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png",
              "fonts/barlow-latin-400-normal.woff2"]:
        assert os.path.isfile(os.path.join(WEB, f)), f
    assert json.loads(read("manifest.webmanifest"))["theme_color"] == "#b4400f"
    assert "gomez-ui v1.1" in read("gomez-ui.css") and ".g-figure" in read("gomez-ui.css")


def test_dockerfile_copies_every_file_the_page_uses():
    docker = read("Dockerfile")
    for f in ["index.html", "app.js", "app.css", "gomez-ui.css", "gomez-ui.js", "manifest.webmanifest",
              "icon.svg", "favicon.ico", "favicon-32.png", "apple-touch-icon.png", "fonts/"]:
        assert f in docker, f


def test_nginx_serves_only_the_data_folder():
    """published/ also holds backup/wiser.db.gz, a copy of the whole database
    (occupancy data). Only published/data/ may reach a browser."""
    conf = read("nginx.conf")
    assert "location ^~ /published/data/ {" in conf
    block = conf.split("location ^~ /published/ {", 1)[1].split("}", 1)[0]
    assert "return 404;" in block
    # ^~ on both, or a regex location (e.g. the icon rule) could still serve a
    # matching file from inside published/.
    assert "location /published/" not in conf
