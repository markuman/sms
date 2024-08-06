import setuptools


def contents(file_name):
    with open(file_name, 'r') as file:
        return file.read()


setuptools.setup(
    name='simple-mbtiles-server',
    version='1.0.0',
    author='markuman',
    author_email='spam@osuv.de',
    description='Server to on-the-fly extract and serve vector tiles from an mbtiles file on fs',
    long_description=contents('README.md'),
    long_description_content_type='text/markdown',
    url='https://github.com/markuman/sms',
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'License :: OSI Approved :: BSD License',
    ],
    python_requires='>=3.7.4',
    install_requires=[
        line for line in
        contents('requirements.txt').splitlines()
        if line and not line.startswith('#')
    ],
    packages=[
        'simple_mbtiles_server',
    ],
    include_package_data=True,
)
