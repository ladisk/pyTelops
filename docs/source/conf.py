# Configuration file for the Sphinx documentation builder.
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import os
import sys

sys.path.insert(0, os.path.abspath("../.."))

# -- Project information -----------------------------------------------------
project = "pyTelops"
author = "Jaša Šonc, Janko Slavič"
copyright = "2026, Jaša Šonc and Ladisk group, University of Ljubljana"
release = "0.2.2"
# -- General configuration ---------------------------------------------------
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinx_llms_txt",
]

# -- sphinx-llms-txt configuration -------------------------------------------
# Emits llms.txt (a Markdown index of every page) and llms-full.txt (the whole
# corpus in one file) into the HTML build output. The URLs inside llms.txt are
# built from html_baseurl, so it must point at the published docs root.
html_baseurl = "https://pytelops.readthedocs.io/en/latest/"
llms_txt_title = "pyTelops Documentation"
llms_txt_summary = (
    "pyTelops is a pure-Python driver for Telops thermal cameras over GigE "
    "Vision, with no vendor SDK required. It speaks the GVCP and GVSP "
    "protocols directly over UDP (on top of pyGigEVision) to connect, grab "
    "calibrated Celsius frames, stream continuously, record to the camera's "
    "onboard buffer, load calibrations, and arm external or software triggers."
)

templates_path = ["_templates"]
exclude_patterns = []

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    "member-order": "bysource",
}
autodoc_mock_imports = ["matplotlib", "PIL", "tqdm"]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
}

napoleon_google_docstring = False
napoleon_numpy_docstring = True
napoleon_use_param = True
napoleon_use_rtype = True

# -- HTML output -------------------------------------------------------------
html_theme = "sphinx_book_theme"
html_static_path = ["_static"]
html_title = "pyTelops"
html_logo = "_static/logo.png"
html_css_files = ["ai-buttons.css"]
html_js_files = ["ai-buttons.js"]
html_theme_options = {
    "repository_url": "https://github.com/ladisk/pyTelops",
    "use_repository_button": True,
    "use_issues_button": True,
    "path_to_docs": "docs/source",
}
