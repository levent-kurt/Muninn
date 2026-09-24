"""Shared sample HTML used by parser tests, API tests, and integration tests."""

GOOGLE_HTML = """
<div id="search"><div id="rso">
  <div class="g">
    <a href="https://docs.python.org/3/"><h3>Python 3 Documentation</h3></a>
    <div class="VwiC3b">Official docs for the Python language.</div>
  </div>
  <div class="g">
    <a href="https://www.python.org/"><h3>Python.org</h3></a>
    <div class="VwiC3b">The official home of the Python Programming Language.</div>
  </div>
</div></div>
"""

BING_HTML = """
<ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://en.wikipedia.org/wiki/Python_(programming_language)">Python (programming language)</a></h2>
    <p class="b_lineclamp4">Python is a high-level, interpreted programming language.</p>
  </li>
  <li class="b_algo">
    <h2><a href="https://example.com/python">Python Tutorial</a></h2>
    <p>Learn Python from scratch.</p>
  </li>
</ol>
"""

DDG_HTML = """
<div class="results">
  <div class="result">
    <a class="result__a" href="https://docs.python.org/3/tutorial/">The Python Tutorial</a>
    <a class="result__snippet" href="https://docs.python.org/3/tutorial/">An informal introduction to Python.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://realpython.com/">Real Python</a>
    <div class="result__snippet">Tutorials for professional developers.</div>
  </div>
</div>
"""

MOJEEK_HTML = """
<ul class="results-standard">
  <li class="result standard">
    <h2><a class="title" href="https://www.mojeek.com/about/">About Mojeek</a></h2>
    <p class="s">Independent search engine with its own index.</p>
  </li>
  <li class="result standard">
    <h2><a class="title" href="https://example.org/py">Python on Example</a></h2>
    <p class="s">A Python overview page.</p>
  </li>
</ul>
"""

# Blocked pages keyed by engine name.
BLOCK_PAGES = {
    "google": """
      <html><body>
        <form id="captcha-form"><div class="g-recaptcha"></div></form>
        <p>Our systems have detected unusual traffic from your computer network.</p>
      </body></html>
    """,
    "bing": "<html><body><h1>Sorry, please verify you are not a robot</h1></body></html>",
    "ddg": "<html><body><h1>Anomaly detected - please solve the captcha</h1></body></html>",
    "mojeek": "<html><body><h1>Too many requests - rate limit exceeded</h1></body></html>",
}

# Successful result HTML keyed by engine name.
ENGINE_RESULTS_HTML = {
    "google": GOOGLE_HTML,
    "bing": BING_HTML,
    "ddg": DDG_HTML,
    "mojeek": MOJEEK_HTML,
}