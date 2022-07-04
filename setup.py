from setuptools import setup, find_packages
import pathlib
import re

WORK_DIR = pathlib.Path(__file__).parent

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

__version__ = ""
exec(open('efb_wechatcom_slave/__version__.py').read())


setup(
    name="efb-wechatcom-slave",
    version=__version__,
    description='EFB Slave for Wechat PC BY COM',
    author='lh900519',
    author_email="undefined@example.com",
    url="https://github.com/lh900519/efb-wechatcom-slave",
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    python_requires='>=3.7',
    keywords=["wechatPc", ],
    install_requires=[
        "ehforwarderbot",
        "lxml",
        "python_magic",
        "PyYAML",
        "requests",
        "websockets",
    ],
    long_description=long_description,
    long_description_content_type="text/markdown",
    classifiers=[
        'Development Status :: 1 - Beta',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: User Interfaces',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.7',
        "Operating System :: OS Independent"
    ],
    entry_points={
        'ehforwarderbot.slave': 'wechat.PcCOM = efb_wechatcom_slave:WechatCOMChannel',
    }
)
