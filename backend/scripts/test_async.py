import asyncio
import time
import httpx

URL = "http://localhost:8000/recognition/ocr"
IMAGE_PATH = "/home/ethics/Downloads/capture_20260211_171657_998860.png"
MACHINE_ID = 1

TOTAL_REQUESTS = 10


async def send_request(client, i):
    with open(IMAGE_PATH, "rb") as f:
        files = {
            "file": (IMAGE_PATH, f, "image/jpeg"),
        }

        data = {"machine_id": str(MACHINE_ID)}

        start = time.time()

        response = await client.post(URL, files=files, data=data)

        elapsed = time.time() - start

        print(f"Request {i} finished in {elapsed:.2f}s | status={response.status_code}")

        return elapsed


async def main():
    async with httpx.AsyncClient(timeout=None) as client:
        tasks = []

        start = time.time()

        for i in range(TOTAL_REQUESTS):
            tasks.append(send_request(client, i))

        results = await asyncio.gather(*tasks)

        total = time.time() - start

        print("\n----- RESULTS -----")
        print(f"Total requests: {TOTAL_REQUESTS}")
        print(f"Total time: {total:.2f}s")
        print(f"Average request time: {sum(results)/len(results):.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
