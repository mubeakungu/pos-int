# This script is a one-time tool to reset the database schema.
# It should only be run if you need to wipe all data and start fresh
# with a new set of SQLAlchemy models.
import os
from app import app, db

with app.app_context():
    print("Dropping all existing tables...")
    db.drop_all()
    print("Creating new tables based on models...")
    db.create_all()
    print("Database schema reset complete.")

    # Re-create the admin user if it's the first run
    from app import User
    if not User.query.filter_by(username='admin').first():
        admin_password = os.environ.get('ADMIN_PASSWORD')
        if not admin_password:
            admin_password = 'admin123'
            print("⚠️  ADMIN_PASSWORD env var not set — using insecure default 'admin123'. Set ADMIN_PASSWORD and change it after first login.")
        admin_user = User(username='admin', password=admin_password, role='admin')
        db.session.add(admin_user)
        db.session.commit()
        print("Default admin user created.")
