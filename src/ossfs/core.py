"""
Code of OSSFileSystem
"""
# pylint:disable=missing-function-docstring
import copy
import logging
import os
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

import oss2
from oss2.auth import AnonymousAuth

from .base import SIMPLE_TRANSFER_THRESHOLD, BaseOSSFileSystem
from .exceptions import translate_oss_error
from .file import OSSFile
from .utils import pretify_info_result

if TYPE_CHECKING:
    from oss2.models import SimplifiedObjectInfo


logger = logging.getLogger("ossfs")


def _as_progress_handler(callback):
    if callback is None:
        return None

    sent_total = False

    def progress_handler(absolute_progress, total_size):
        nonlocal sent_total
        if not sent_total:
            callback.set_size(total_size)
            sent_total = True

        callback.absolute_update(absolute_progress)

    return progress_handler


class OSSFileSystem(BaseOSSFileSystem):  # pylint:disable=too-many-public-methods
    # pylint:disable=no-value-for-parameter
    """
    A pythonic file-systems interface to OSS (Object Storage Service)

    Examples
    --------
    >>> ossfs = OSSFileSystem(anon=False)
    >>> ossfs.ls('my-bucket/')
    ['my-file.txt']

    >>> with ossfs.open('my-bucket/my-file.txt', mode='rb') as f:
    ...     print(f.read())
    b'Hello, world!'
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._session = oss2.Session()

    def _get_bucket(
        self, bucket_name: str, connect_timeout: Optional[int] = None
    ) -> oss2.Bucket:
        """
        get the new bucket instance
        """
        if not self._endpoint:
            raise ValueError("endpoint is required")
        try:
            return oss2.Bucket(
                self._auth,
                self._endpoint,
                bucket_name,
                session=self._session,
                connect_timeout=connect_timeout,
                app_name="ossfs",
            )
        except oss2.exceptions.ClientError as err:
            raise ValueError(bucket_name) from err

    def _call_oss(
        self,
        method_name: str,
        *args,
        bucket: Optional[str] = None,
        timeout: Optional[int] = None,
        retry: int = 3,
        **kwargs,
    ):
        if bucket:
            service = self._get_bucket(bucket, timeout)
        else:
            service = oss2.Service(
                self._auth,
                endpoint=self._endpoint,
                connect_timeout=timeout,
            )
        for count in range(retry):
            try:
                method = getattr(service, method_name, None)
                if not method:
                    method = getattr(oss2, method_name)
                    logger.debug("CALL: %s - %s - %s", method.__name__, args, kwargs)
                    out = method(service, *args, **kwargs)
                else:
                    logger.debug("CALL: %s - %s - %s", method.__name__, args, kwargs)
                    out = method(*args, **kwargs)
                return out
            except oss2.exceptions.RequestError as err:
                logger.debug("Retryable error: %s, try %s times", err, count + 1)
                error = err
            except oss2.exceptions.OssError as err:
                logger.debug("Nonretryable error: %s", err)
                error = err
                break
        raise translate_oss_error(error)

    def _open(
        self,
        path: str,
        mode: str = "rb",
        block_size: Optional[int] = None,
        autocommit: bool = True,
        cache_options: Optional[str] = None,
        **kwargs,  # pylint: disable=too-many-arguments
    ) -> "OSSFile":
        """
        Open a file for reading or writing.
        Parameters
        ----------
        path: str
            File location
        mode: str
            'rb', 'wb', etc.
        autocommit: bool
            If False, writes to temporary file that only gets put in final
            location upon commit
        kwargs
        Returns
        -------
        OSSFile instance
        """
        cache_type = kwargs.pop("cache_type", self._default_cache_type)
        return OSSFile(
            self,
            path,
            mode,
            block_size,
            autocommit,
            cache_options=cache_options,
            cache_type=cache_type,
            **kwargs,
        )

    def _ls_bucket(self, connect_timeout: Optional[int]) -> List[Dict[str, Any]]:
        if "" not in self.dircache:
            results: List[Dict[str, Any]] = []
            if isinstance(self._auth, AnonymousAuth):
                logging.warning("cannot list buckets if not logged in")
                return []
            try:
                for bucket in self._call_oss("BucketIterator", timeout=connect_timeout):
                    result = {
                        "name": bucket.name,
                        "type": "directory",
                        "size": 0,
                        "CreateTime": bucket.creation_date,
                    }
                    results.append(result)
            except oss2.exceptions.ClientError:
                pass
            self.dircache[""] = copy.deepcopy(results)
        else:
            results = self.dircache[""]
        return results

    def _get_object_info_list(
        self,
        bucket_name: str,
        prefix: str,
        delimiter: str,
        connect_timeout: Optional[int],
    ):
        """
        Wrap oss2.ObjectIterator return values into a
        fsspec form of file info
        """
        result = []
        obj: "SimplifiedObjectInfo"
        for obj in self._call_oss(
            "ObjectIterator",
            prefix=prefix,
            delimiter=delimiter,
            bucket=bucket_name,
            timeout=connect_timeout,
        ):
            data = self._transfer_object_info_to_dict(bucket_name, obj)
            result.append(data)
        return result

    def _ls_dir(
        self,
        path: str,
        delimiter: str = "/",
        refresh: bool = False,
        prefix: str = "",
        connect_timeout: Optional[int] = None,
        **kwargs,  # pylint: disable=too-many-arguments
    ) -> List[Dict]:
        norm_path = path.strip("/")
        if norm_path in self.dircache and not refresh and not prefix and delimiter:
            return self.dircache[norm_path]

        logger.debug("Get directory listing page for %s", norm_path)
        bucket_name, key = self.split_path(norm_path)
        if not delimiter or prefix:
            if key:
                prefix = f"{key}/{prefix}"
        else:
            if norm_path in self.dircache and not refresh:
                return self.dircache[norm_path]
            if key:
                prefix = f"{key}/"

        try:
            self.dircache[norm_path] = self._get_object_info_list(
                bucket_name, prefix, delimiter, connect_timeout
            )
            return self.dircache[norm_path]
        except oss2.exceptions.AccessDenied:
            return []

    @pretify_info_result
    def ls(self, path: str, detail: bool = True, **kwargs):
        connect_timeout = kwargs.pop("connect_timeout", 60)
        norm_path = self._strip_protocol(path).strip("/")
        if norm_path == "":
            return self._ls_bucket(connect_timeout)
        files = self._ls_dir(path, connect_timeout=connect_timeout)
        if not files and "/" in norm_path:
            files = self._ls_dir(self._parent(path), connect_timeout=connect_timeout)
            files = [
                file
                for file in files
                if file["type"] != "directory" and file["name"].strip("/") == norm_path
            ]

        return files

    @pretify_info_result
    def find(
        self,
        path: str,
        maxdepth: Optional[int] = None,
        withdirs: bool = False,
        detail: bool = False,
        **kwargs,
    ):
        """List all files below path.

        Like posix ``find`` command without conditions

        Parameters
        ----------
        path : str
        maxdepth: int or None
            If not None, the maximum number of levels to descend
        withdirs: bool
            Whether to include directory paths in the output. This is True
            when used by glob, but users usually only want files.
        kwargs are passed to ``ls``.
        """
        path = self._strip_protocol(path)
        out = {}
        prefix = kwargs.pop("prefix", "")
        if (withdirs or maxdepth) and prefix:
            raise ValueError(
                "Can not specify 'prefix' option alongside "
                "'withdirs'/'maxdepth' options."
            )
        if prefix:
            connect_timeout = kwargs.get("connect_timeout", None)
            for info in self._ls_dir(
                path, delimiter="", prefix=prefix, connect_timeout=connect_timeout
            ):
                out.update({info["name"]: info})
        else:
            for _, dirs, files in self.walk(path, maxdepth, detail=True, **kwargs):
                if withdirs:
                    files.update(dirs)
                out.update({info["name"]: info for name, info in files.items()})
            if self.isfile(path) and path not in out:
                # walk works on directories, but find should also return [path]
                # when path happens to be a file
                out[path] = {}
        names = sorted(out)
        return {name: out[name] for name in names}

    def _directory_exists(self, dirname: str, **kwargs):
        connect_timeout = kwargs.pop("connect_timeout", None)
        ls_result = self._ls_dir(dirname, connect_timeout=connect_timeout)
        return bool(ls_result)

    def _bucket_exist(self, bucket_name: str):
        if not bucket_name:
            return False
        try:
            self._call_oss("get_bucket_info", bucket=bucket_name)
        except (oss2.exceptions.OssError, PermissionError):
            return False
        return True

    def exists(self, path: str, **kwargs) -> bool:
        """Is there a file at the given path"""
        norm_path = self._strip_protocol(path).lstrip("/")
        if norm_path == "":
            return True

        bucket_name, obj_name = self.split_path(path)

        if not self._bucket_exist(bucket_name):
            return False

        connect_timeout = kwargs.get("connect_timeout", None)
        if not obj_name:
            return True

        if self._call_oss(
            "object_exists",
            obj_name,
            bucket=bucket_name,
            timeout=connect_timeout,
        ):
            return True

        return self._directory_exists(path, **kwargs)

    def ukey(self, path: str):
        """Hash of file properties, to tell if it has changed"""
        bucket_name, obj_name = self.split_path(path)
        obj_stream = self._call_oss("get_object", obj_name, bucket=bucket_name)
        return obj_stream.server_crc

    def checksum(self, path: str):
        """Unique value for current version of file

        If the checksum is the same from one moment to another, the contents
        are guaranteed to be the same. If the checksum changes, the contents
        *might* have changed.

        This should normally be overridden; default will probably capture
        creation/modification timestamp (which would be good) or maybe
        access timestamp (which would be bad)
        """
        return sha256(
            (str(self.ukey(path)) + str(self.info(path))).encode()
        ).hexdigest()

    def cp_file(self, path1: str, path2: str, **kwargs):
        """
        Copy within two locations in the filesystem
        # todo: big file optimization
        """
        bucket_name1, obj_name1 = self.split_path(path1)
        bucket_name2, obj_name2 = self.split_path(path2)
        if bucket_name1 != bucket_name2:
            tempdir = "." + self.ukey(path1)
            self.get_file(path1, tempdir, **kwargs)
            self.put_file(tempdir, path2, **kwargs)
            os.remove(tempdir)
        else:
            connect_timeout = kwargs.pop("connect_timeout", None)
            self._call_oss(
                "copy_object",
                bucket_name1,
                obj_name1,
                obj_name2,
                bucket=bucket_name1,
                timeout=connect_timeout,
            )
        self.invalidate_cache(self._parent(path2))

    def _rm(self, path: Union[str, List[str]]):
        """Delete files.

        Parameters
        ----------
        path: str or list of str
            File(s) to delete.
        """
        if isinstance(path, list):
            for file in path:
                self._rm(file)
            return
        bucket_name, obj_name = self.split_path(path)
        self._call_oss("delete_object", obj_name, bucket=bucket_name)
        self.invalidate_cache(self._parent(path))

    def rm(self, path: Union[str, List[str]], recursive=False, maxdepth=None):
        """Delete files.

        Parameters
        ----------
        path: str or list of str
            File(s) to delete.
        recursive: bool
            If file(s) are directories, recursively delete contents and then
            also remove the directory
        maxdepth: int or None
            Depth to pass to walk for finding files to delete, if recursive.
            If None, there will be no limit and infinite recursion may be
            possible.
        """

        if isinstance(path, list):
            for file in path:
                self.rm(file)
            return

        bucket_name, _ = self.split_path(path)
        path_expand = self.expand_path(path, recursive=recursive, maxdepth=maxdepth)
        path_expand = [self.split_path(file)[1] for file in path_expand]

        def chunks(lst: list, num: int):
            for i in range(0, len(lst), num):
                yield lst[i : i + num]

        for files in chunks(path_expand, 1000):
            self._call_oss("batch_delete_objects", files, bucket=bucket_name)

        self.invalidate_cache(self._parent(path))

    def get_path(self, rpath: str, lpath: str, **kwargs):
        """
        Copy single remote path to local
        """
        if self.isdir(rpath):
            os.makedirs(lpath, exist_ok=True)
        else:
            self.get_file(rpath, lpath, **kwargs)

    def get_file(
        self, rpath: str, lpath: str, callback: Optional[Callable] = None, **kwargs
    ):  # pylint: disable=arguments-differ
        """
        Copy single remote file to local
        """
        kwargs.setdefault("progress_callback", _as_progress_handler(callback))
        if self.isdir(rpath):
            os.makedirs(lpath, exist_ok=True)
        else:
            bucket_name, obj_name = self.split_path(rpath)
            connect_timeout = kwargs.pop("connect_timeout", None)
            bucket = self._get_bucket(bucket_name, connect_timeout)
            if self.size(rpath) >= SIMPLE_TRANSFER_THRESHOLD:
                oss2.resumable_download(bucket, obj_name, lpath, **kwargs)
            else:
                self._call_oss(
                    "get_object_to_file",
                    obj_name,
                    lpath,
                    bucket=bucket_name,
                    timeout=connect_timeout,
                    **kwargs,
                )

    def put_file(
        self, lpath: str, rpath: str, callback: Optional[Callable] = None, **kwargs
    ):  # pylint: disable=arguments-differ
        """
        Copy single file to remote
        """
        kwargs.setdefault("progress_callback", _as_progress_handler(callback))
        if os.path.isdir(lpath):
            self.makedirs(rpath, exist_ok=True)
        else:
            bucket_name, obj_name = self.split_path(rpath)
            connect_timeout = kwargs.pop("connect_timeout", None)
            bucket = self._get_bucket(bucket_name, connect_timeout)
            if os.path.getsize(lpath) >= SIMPLE_TRANSFER_THRESHOLD:
                oss2.resumable_upload(bucket, obj_name, lpath, **kwargs)
            else:
                self._call_oss(
                    "put_object_from_file",
                    obj_name,
                    lpath,
                    bucket=bucket_name,
                    timeout=connect_timeout,
                    **kwargs,
                )
        self.invalidate_cache(self._parent(rpath))

    def created(self, path: str):
        """Return the created timestamp of a file as a datetime.datetime"""
        bucket_name, obj_name = self.split_path(path)
        if obj_name:
            raise NotImplementedError("OSS has no created timestamp")
        bucket_info = self._call_oss("get_bucket_info", bucket=bucket_name)
        timestamp = bucket_info.creation_date
        return datetime.fromtimestamp(timestamp)

    def modified(self, path: str):
        """Return the modified timestamp of a file as a datetime.datetime"""
        bucket_name, obj_name = self.split_path(path)
        if not obj_name or self.isdir(path):
            raise NotImplementedError("bucket has no modified timestamp")
        simplifiedmeta = self._call_oss("get_object_meta", obj_name, bucket=bucket_name)
        return int(
            datetime.strptime(
                simplifiedmeta.headers["Last-Modified"],
                "%a, %d %b %Y %H:%M:%S %Z",
            ).timestamp()
        )

    def append_object(self, path: str, location: int, value: bytes) -> int:
        """
        Append bytes to the object
        """
        bucket_name, obj_name = self.split_path(path)
        result = self._call_oss(
            "append_object",
            obj_name,
            location,
            value,
            bucket=bucket_name,
        )
        return result.next_position

    def get_object(self, path: str, start: int, end: int) -> bytes:
        """
        Return object bytes in range
        """
        headers = {"x-oss-range-behavior": "standard"}
        bucket_name, obj_name = self.split_path(path)
        try:
            object_stream = self._call_oss(
                "get_object",
                obj_name,
                bucket=bucket_name,
                byte_range=(start, end),
                headers=headers,
            )
        except oss2.exceptions.ServerError as err:
            raise err
        return object_stream.read()

    def sign(self, path: str, expiration: int = 100, **kwargs):
        raise NotImplementedError("Sign is not implemented for this filesystem")

    def touch(self, path: str, truncate: bool = True, **kwargs):
        """Create empty file, or update timestamp

        Parameters
        ----------
        path: str
            file location
        truncate: bool
            If True, always set file size to 0; if False, update timestamp and
            leave file unchanged, if backend allows this
        """
        if truncate or not self.exists(path):
            with self.open(path, "wb", **kwargs):
                pass
            self.invalidate_cache(self._parent(path))

    def pipe_file(self, path: str, value: str, **kwargs):
        """Set the bytes of given file"""
        bucket_name, obj_name = self.split_path(path)
        self._call_oss("put_object", obj_name, value, bucket=bucket_name, **kwargs)
        bucket = self._get_bucket(bucket_name)
        bucket.put_object(obj_name, value, **kwargs)
        self.invalidate_cache(self._parent(path))

    @pretify_info_result
    def info(self, path, **kwargs):
        norm_path = self._strip_protocol(path).lstrip("/")
        if norm_path == "":
            result = {"name": path, "size": 0, "type": "directory"}
        else:
            result = super().info(path, **kwargs)
        if "StorageClass" in result:
            del result["StorageClass"]
        if "CreateTime" in result:
            del result["CreateTime"]
        return result
