import asyncio


async def main() -> None:
    proc = await asyncio.create_subprocess_exec("./test.sh")
    _stdout, _stderr = await proc.communicate()
    assert proc.returncode == 0


if __name__ == "__main__":
    asyncio.run(main())
