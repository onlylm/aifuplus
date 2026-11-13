"""Dev/simple production entry: python run.py  (also starts the background sweeper thread)."""
import os

from app import create_app
from app.migrate import migrate

if __name__ == "__main__":
    migrate()
    app = create_app(start_worker=True)
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), threaded=True)
