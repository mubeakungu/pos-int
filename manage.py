# manage.py

import os
import sys
from flask.cli import FlaskGroup
from app import app, db, User, Product, Sale
from flask_migrate import init

def create_app():
    return app

cli = FlaskGroup(create_app=create_app)

@cli.command('init_db')
def init_db():
    """Initializes the database and migration environment."""
    with app.app_context():
        # Initialize Flask-Migrate's scripts folder
        # This creates the 'migrations' directory and 'alembic.ini' file
        try:
            init(directory='migrations')
            print("✅ Migration environment initialized.")
        except Exception as e:
            print(f"❌ Error initializing migration environment: {e}")
            sys.exit(1)

        # Create a default admin user
        existing_admin = User.query.filter_by(username='admin').first()
        if not existing_admin:
            admin_password = os.environ.get('ADMIN_PASSWORD')
            if not admin_password:
                admin_password = 'admin123'
                print("⚠️  ADMIN_PASSWORD env var not set — using insecure default 'admin123'. Set ADMIN_PASSWORD and change it after first login.")
            admin_user = User(username='admin', password=admin_password, role='admin')
            db.session.add(admin_user)
            db.session.commit()
            print("✅ Default admin user created.")
        else:
            print("✅ Admin user already exists.")

if __name__ == '__main__':
    cli()
