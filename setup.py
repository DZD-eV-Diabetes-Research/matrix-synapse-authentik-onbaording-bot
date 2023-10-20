from setuptools import setup, find_packages

# Read the requirements from reqs.txt
with open("reqs.txt") as f:
    install_requires = [
        line.strip() for line in f if line.strip() and not line.startswith("#")
    ]

setup(
    name="my_project",
    version="0.1.0",
    packages=find_packages(),
    install_requires=install_requires,
)
