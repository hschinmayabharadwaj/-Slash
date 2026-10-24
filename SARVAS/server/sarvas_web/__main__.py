"""Start the web server:  python -m sarvas_web [path/to/config.json]"""
import sys

from .server import main

main(sys.argv)
