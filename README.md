# Employee Attendance & Analytics API

A REST API built using Python, FastAPI and MongoDB for employee management, attendance tracking and analytics.

## Technology Stack

* Python 3.11+
* FastAPI
* MongoDB
* PyMongo

## Features

* Employee creation
* Attendance punch-in and punch-out
* Attendance records
* Analytics endpoints
* MongoDB database integration
* Interactive API documentation using Swagger UI

## Setup

Install the dependencies:

```bash
pip install -r requirements.txt
```

Configure the `MONGO_URI` and `MONGO_DB` environment variables, then start the application:

```bash
uvicorn app.main:app --port 8000
```

Open `http://localhost:8000/docs` to explore the API.

## Security

Database credentials and environment files must not be committed to the repository.

## Verification Status

Basic application startup, database connectivity, employee creation and punch-in were tested. Other business rules and analytics endpoints require further verification. Refer to the implementation and API contract for details.
