# V-Cafe Python backend

The backend is implemented in Python and stores registrations, reservations,
feedback, contact messages, and newsletter subscriptions in MySQL.

## Setup

1. Install Python 3 and MySQL Server.
2. In MySQL Workbench, configure the local connection with hostname
   `127.0.0.1`, port `3306`, and username `root`.
3. Copy `.env.example` to `.env` in this folder and set `MYSQL_PASSWORD` to the
   password for your local MySQL account. Keep real credentials in `.env`;
   `.env` is excluded from version control.
4. Run the project's `start_backend.bat`. The launcher installs the Python
   dependency from `requirements.txt` if it is missing, then starts the server.
   Alternatively, install it with `python -m pip install -r requirements.txt`
   and start the server with `python server.py`.

On startup, the backend creates the configured database (default: `vcafe_db`)
and its `vcafe_*` tables if they do not already exist. Existing records remain
in place. Reservations are stored in separate columns such as `guest_name`,
`res_date`, and `bill_amount`; existing reservation JSON records are migrated
to these columns on startup. Other submission types retain their existing
payload storage. The site and API are served at `http://localhost:5000`; check
the MySQL connection at `http://localhost:5000/api/health`.

The API supports registrations, reservations, feedback, contact messages,
newsletter subscriptions, and the admin submissions view. Registration
passwords are stored as salted scrypt hashes.
