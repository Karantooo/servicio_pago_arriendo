import os
import tempfile


_test_database_dir = tempfile.TemporaryDirectory(prefix="payments-tests-")
os.environ["PAYMENTS_DATABASE_URL"] = (
    f"sqlite:///{_test_database_dir.name}/payments.sqlite"
)
os.environ["PAYMENTS_PROVIDER"] = "mock"
