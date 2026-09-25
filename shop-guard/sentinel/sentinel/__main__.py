import logging

import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
uvicorn.run("sentinel.web:app", host="0.0.0.0", port=8080, log_level="info")
