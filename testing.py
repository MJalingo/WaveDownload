from fastapi import FastAPI
from fastapi.testclient import TestClient


app = FastAPI()


@app.get("/")
def read_root():
	return {"message": "Hello, FastAPI!"}


@app.get("/items/{item_id}")
def read_item(item_id: int):
	return {"item_id": item_id}


client = TestClient(app)


def test_read_root():
	response = client.get("/")
	assert response.status_code == 200
	assert response.json() == {"message": "Hello, FastAPI!"}


def test_read_item():
	response = client.get("/items/42")
	assert response.status_code == 200
	assert response.json() == {"item_id": 42}


def test_read_item_rejects_invalid_id():
	response = client.get("/items/not-an-id")
	assert response.status_code == 422


if __name__ == "__main__":
	import uvicorn

	uvicorn.run(app, host="127.0.0.1", port=8000)