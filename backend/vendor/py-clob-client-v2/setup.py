from setuptools import find_packages, setup


setup(
    name="py-clob-client-v2",
    version="1.0.0+local",
    description="Python client for the Polymarket CLOBV2",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="Polymarket Engineering",
    author_email="engineering@polymarket.com",
    maintainer="Polymarket Engineering",
    maintainer_email="engineering@polymarket.com",
    url="https://github.com/Polymarket/py-clob-client-v2",
    python_requires=">=3.9.10",
    packages=find_packages(include=["py_clob_client_v2", "py_clob_client_v2.*"]),
    install_requires=[
        "eth-account>=0.13.0",
        "eth-utils>=4.1.1",
        "poly_eip712_structs>=0.0.1",
        "py-order-utils>=0.3.2",
        "httpx[http2]>=0.27.0",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
