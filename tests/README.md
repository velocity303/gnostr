# gnostr Unit Test Guide

This directory contains the unit tests for the `gnostr` application, designed using a **Test-Driven Development (TDD)** approach to ensure that every critical business rule lives beable and isolated.

## 🧪 Modules Covered
1.  **`test_gateway.py`**: Tests the data contracts defined in `src/gateway/*.py`. These verify that our Repositories behave correctly, regardless of whether they use SQLite, Postgres, or a mock object.
2.  **`test_profile_service.py`**: Tests the core business logic from `ProfileService`. It confirms that services correctly orchestrate multiple underlying data sources (e.g., fetching profile *and* recent activity) to assemble a coherent state object.

## 🚀 How to Run These Tests
### **⚠️ WARNING: Environment Setup**
Due to Python's module path system, running these tests requires the entire `gnostr` directory structure to be recognized as an installed or editable package in your virtual environment. Running `pytest` from outside the root may fail with a `ModuleNotFoundError`.

**Recommended Command (from `/home/james/Projects/gnostr`):**
```bash
# Ensure you are in the main project root: /home/james/Projects/gnostr
pip install -e . # If pip is used to manage dependencies
pytest tests/ 
```

### **✅ Interpreting Results**
*   **Success:** All tests pass (green output). This means that, according to our defined interfaces and service logic, the core functionalities are provably correct.
*   **Failure:** A failure indicates a logical flaw in:
    1.  The Service Layer's business rule execution (`ProfileService`).
    2.  An incorrect assumption about data retrieval from the Gateway/Database layer.

*Note: The `conftest.py` fixtures handle Dependency Injection, allowing separate tests to manipulate and confirm behavior against mock services.*