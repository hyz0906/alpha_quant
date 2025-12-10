import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from src.database.connection import engine, Base
from src.database import models

def init_db():
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)
    print("Tables created successfully.")

if __name__ == "__main__":
    init_db()
