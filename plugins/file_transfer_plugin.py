import errno
import logging
import pysftp
import os
import subprocess

from airflow.plugins_manager import AirflowPlugin

from airflow.contrib.hooks.ftp_hook import FTPHook
from airflow.exceptions import AirflowException
from airflow.hooks import BaseHook
from airflow.hooks.S3_hook import S3Hook
from airflow.models import BaseOperator
from airflow.operators.sensors import BaseSensorOperator
from airflow.utils.decorators import apply_defaults
from past.builtins import basestring

from contextlib import contextmanager
from io import BytesIO
from os.path import exists
from os.path import isdir
from shutil import copyfile, copytree, rmtree
from tempfile import mkstemp, NamedTemporaryFile


try:
    FileExistsError
except NameError:
    class FileExistsError (Exception):
        pass


def makedirs(path, exist_ok=False):
    """
    Compatibility function to emulate Python 3.2+'s `exist_ok` argument.
    """
    try:
        os.makedirs(path)
        return True
    except OSError as e:
        if not (exist_ok and e.errno == errno.EEXIST and isdir(path)):
            raise
    except FileExistsError:
        if not (exist_ok and isdir(path)):
            raise
    return False


class CommonFileHook (BaseHook):
    """
    Base class for hooks that implement the common file interface. Provides
    an interface for opening a file for streaming, upload from and download to
    a local file system, deleting files, checking existence, etc.
    """
    def parse_mode(self, mode):
        return {
            'is_binary': 'b' in mode,
            'can_read': not mode or mode.startswith('r'),
            'can_write': mode.startswith('w') or
                        mode.startswith('r+') or
                        mode.startswith('a'),
            'replace': mode.startswith('w'),
        }

    @staticmethod
    def by_type(conn_type):
        return {
            'ftp': CommonFTPHook,
            'sftp': CommonFTPSHook,
            's3': CommonS3Hook,
            'local': CommonFSHook,
        }[conn_type]

    @contextmanager
    def open(self, remotepath, mode='r'):
        """
        Return a file-like object.

        :param remotepath: The path or URL to the file to be opened
        :param mode: The hook should implement identical semantics as Python's
                     `open` function:
                     https://docs.python.org/3/tutorial/inputoutput.html#reading-and-writing-files
        """
        raise NotImplementedError

    def download(self, remotepath, localpath, replace=True):
        """
        Transfer a file from a remote source to a local file path.
        """
        raise NotImplementedError

    def download_folder(self, remotepath, localpath, replace=True):
        """
        Transfer everything in a folder at a remote source to a local folder.
        """
        raise NotImplementedError

    def upload(self, localpath, remotepath, replace=True):
        """
        Transfer a file from a local file path to a remote destination.
        """
        raise NotImplementedError

    def delete(self, remotepath):
        """
        Delete a file from a remote source.
        """
        raise NotImplementedError

    def file_exists(self, remotepath):
        raise NotImplementedError

    def folder_exists(self, remotepath):
        raise NotImplementedError

# ---------------------------------------------------------

class CommonFSHook (CommonFileHook):
    def __init__(self, conn_id=None):
        pass

    def download(self, remotepath, localpath, replace=True):
        if not replace and exists(localpath):
            raise FileExistsError(localpath)
        copyfile(remotepath, localpath)

    def download_folder(self, remotepath, localpath, replace=True):
        if exists(localpath):
            if replace: rmtree(localpath)
            else: raise FileExistsError(localpath)
        copytree(remotepath, localpath)

    def upload(self, localpath, remotepath, replace=True):
        if not replace and exists(remotepath):
            raise FileExistsError(remotepath)
        copyfile(localpath, remotepath)

    def delete(self, path, is_dir=None):
        if is_dir is None:
            is_dir = isdir(path)

        if is_dir:
            from shutil import rmtree
            rmtree(path)
        else:
            from os import unlink
            unlink(path)

    @contextmanager
    def open(self, remotepath, mode='rb'):
        return open(remotepath, mode)

# ---------------------------------------------------------

class CommonFTPHookMixin (CommonFileHook):
    def download(self, remotepath, localpath, replace=True):
        if not replace and exists(localpath):
            raise FileExistsError(localpath)
        dirname = os.path.dirname(localpath)
        logging.info('Creating the folder {}, if it does not exist'.format(dirname))
        if makedirs(dirname, exist_ok=True):
            logging.info('Created!')
        else:
            logging.info('Already existed.')

        self.retrieve_file(remotepath, localpath)

    def download_folder(self, remotepath, localpath, replace=True):
        if exists(localpath):
            if replace: rmtree(localpath)
            else: raise FileExistsError(localpath)
        logging.info('Creating the folder {}, if it does not exist'.format(localpath))
        if makedirs(localpath, exist_ok=True):
            logging.info('Created!')
        else:
            logging.info('Already existed.')
        self.retrieve_folder(remotepath, localpath)

    def upload(self, localpath, remotepath, replace=True):
        if not replace:
            # TODO: Implement logic to check whether remote path exists
            pass
        self.store_file(remotepath, localpath)

    @contextmanager
    def open(self, remotepath, mode='r'):
        mode_params = self.parse_mode(mode)
        if mode_params['can_read'] and mode_params['can_write']:
            raise NotImplementedError('Cannot open a read/write FTP stream.')
        elif mode_params['can_write'] and not mode_params['replace']:
            raise NotImplementedError('Cannot append to a file over FTP.')
        elif mode_params['can_read']:
            return self._open_for_read(remotepath, **mode_params)
        elif mode_params['can_write']:
            return self._open_for_write(remotepath, **mode_params)

    def _open_for_read(self, remotepath, **mode_params):
        fileob = BytesIO()
        self.retrieve_file(path, fileob)
        yield fileob

    def _open_for_write(self, remotepath, **mode_params):
        fileob = BytesIO()
        self.store_file(remotepath, fileob)
        yield fileob

class PySFTPHook (FTPHook):
    """
    An alternative to the FTPSHook, which doesn't support as many connection
    protocols as pysftp, which is built on paramiko. The pysftp Connection
    objects has a different interface than the ftplib Connection, so this class
    ends up reimplementing many methods.
    """

    def get_conn(self):
        """
        Returns a PySFTP connection object
        """
        if self.conn is None:
            logging.info("Using Connection with ID {}".format(self.ftp_conn_id))
            params = self.get_connection(self.ftp_conn_id)
            logging.info('Establishing secure connection to {}'.format(params.host))
            self.conn = pysftp.Connection   (
                params.host, username=params.login, password=params.password
            )
        return self.conn

    def retrieve_file(self, remote_full_path, local_full_path_or_buffer):
        conn = self.get_conn()

        is_path = isinstance(local_full_path_or_buffer, basestring)

        if is_path:
            output_handle = open(local_full_path_or_buffer, 'wb')
        else:
            output_handle = local_full_path_or_buffer

        logging.info('Retrieving file from FTP: {}'.format(remote_full_path))
        conn.getfo(remote_full_path, output_handle)
        logging.info('Finished retrieving file from FTP: {}'.format(
            remote_full_path))

        if is_path:
            output_handle.close()

    def retrieve_folder(self, remote_full_path, local_full_path):
        conn = self.get_conn()

        logging.info('Retrieving files in folder from FTP: {}'.format(remote_full_path))
        conn.get_d(remote_full_path, local_full_path)
        logging.info('Finished retrieving folder from FTP: {}'.format(
            remote_full_path))

    def _match_basename(self, dirname, basepattern, modefilter=None):
        import stat
        from fnmatch import fnmatch

        conn = self.get_conn()
        modefilter = modefilter or (lambda mode: True)

        logging.info('Changing into folder {} to look for pattern {}'.format(dirname, basepattern))
        with conn.cd(dirname):
            basenames = [attrs.filename for attrs in conn.listdir_attr()
                         if modefilter(attrs.st_mode) and fnmatch(attrs.filename, basepattern)]
        logging.info('Found the following files: {}'.format(basenames))
        return basenames

    def file_exists(self, remote_full_path):
        """
        Check if a file exists. A pySFTP connection has an `isfile` method, but
        we search a directory listing instead, so that we can support wildcard
        matches.
        """
        from stat import S_ISREG
        pathhead, pathtail = os.path.split(remote_full_path)
        basenames = self._match_basename(pathhead, pathtail, modefilter=S_ISREG)
        return len(basenames) > 0

    def folder_exists(self, remote_full_path):
        """
        Check if a folder exists. A pySFTP connection has an `isdir` method, but
        we search a directory listing instead, so that we can support wildcard
        matches.
        """
        from stat import S_ISDIR
        pathhead, pathtail = os.path.split(remote_full_path.rstrip('/'))
        basenames = self._match_basename(pathhead, pathtail, modefilter=S_ISDIR)
        return len(basenames) > 0

class CommonFTPHook (FTPHook, CommonFTPHookMixin):
    def __init__(self, conn_id):
        FTPHook.__init__(self, ftp_conn_id=conn_id)

class CommonFTPSHook (PySFTPHook, CommonFTPHookMixin):
    def __init__(self, conn_id):
        PySFTPHook.__init__(self, ftp_conn_id=conn_id)

# ---------------------------------------------------------

class CommonS3Hook (S3Hook, CommonFileHook):
    def __init__(self, conn_id):
        S3Hook.__init__(self, s3_conn_id=conn_id)

    @contextmanager
    def open(self, remotepath, mode='r'):
        s3_obj = self.get_key(path)
        with NamedTemporaryFile() as fileob:
            s3_obj.get_contents_to_file(fileob)
            yield fileob


# =========================================================
# Operators

class FileDownloadOperator(BaseOperator):
    """
    Downloads a file from a connection.
    """
    template_fields = ('source_path','dest_path',)
    template_ext = ()
    ui_color = '#ffcc44'

    @apply_defaults
    def __init__(self,
                 source_type,
                 source_path,
                 source_conn_id=None,
                 dest_path=None,
                 *args, **kwargs):
        super(FileDownloadOperator, self).__init__(*args, **kwargs)

        self.source_type = source_type
        self.source_conn_id = source_conn_id
        self.source_path = source_path

        self.SourceHook = CommonFileHook.by_type(source_type)

        self.dest_path = dest_path

    def execute(self, context):
        if not self.dest_path:
            self.create_temp_dest()
        self.download_source()

        # Return the destination path as an xcom variable
        return self.dest_path

    def create_temp_dest(self):
        try:
            dest = mkstemp()
            self.dest_path = dest.name
            dest.close()
        except e:
            raise AirflowException("Failed to create temporary file for download: {}".format(e))
        else:
            logging.info("Created a temporary file for download at {}".format(self.dest_path))

    def download_source(self):
        logging.info("Downloading file {} from {} source {} to local file {}."
            .format(self.source_path, self.source_type, self.source_conn_id, self.dest_path))
        self.SourceHook(self.source_conn_id).download(self.source_path, self.dest_path)

class FolderDownloadOperator(BaseOperator):
    """
    Downloads a folder from a connection.
    """
    template_fields = ('source_path','dest_path',)
    template_ext = ()
    ui_color = '#ffcc44'

    @apply_defaults
    def __init__(self,
                 source_type,
                 source_path,
                 source_conn_id=None,
                 dest_path=None,
                 *args, **kwargs):
        super(FolderDownloadOperator, self).__init__(*args, **kwargs)

        self.source_type = source_type
        self.source_conn_id = source_conn_id
        self.source_path = source_path

        self.SourceHook = CommonFileHook.by_type(source_type)

        self.dest_path = dest_path

    def execute(self, context):
        if not self.dest_path:
            self.create_temp_dest()
        self.download_source()

        # Return the destination path as an xcom variable
        return self.dest_path

    def create_temp_dest(self):
        try:
            dest = mkstemp()
            self.dest_path = dest.name
            dest.close()
        except e:
            raise AirflowException("Failed to create temporary folder for download: {}".format(e))
        else:
            logging.info("Created a temporary folder for download at {}".format(self.dest_path))

    def download_source(self):
        logging.info("Downloading folder {} from {} source {} to local folder {}."
            .format(self.source_path, self.source_type, self.source_conn_id, self.dest_path))
        self.SourceHook(self.source_conn_id).download_folder(self.source_path, self.dest_path)

class FileTransferOperator(BaseOperator):
    """
    Transfers a file from one connection to another.

    :param source_conn_id: The connection to run the operator against.
    :type source_conn_id: string
    :param source_path: The path to the file on the server
    :type source_path: string
    :param dest_path: The full path to which the file will be downloaded
    :type dest_path: string
    :param secure: Whether to use a secure connection
    :type secure: bool
    """

    template_fields = ('source_path','dest_path',)
    template_ext = ()
    ui_color = '#ffcc44'

    @apply_defaults
    def __init__(self,
                 source_type,
                 dest_type,
                 source_path,
                 dest_path,
                 source_conn_id=None,
                 dest_conn_id=None,
                 *args, **kwargs):
        super(FileTransferOperator, self).__init__(*args, **kwargs)

        self.source_type = source_type
        self.source_conn_id = source_conn_id
        self.source_path = source_path

        self.SourceHook = CommonFileHook.by_type(source_type)

        self.dest_type = dest_type
        self.dest_conn_id = dest_conn_id
        self.dest_path = dest_path

        self.DestHook = CommonFileHook.by_type(dest_type)

    def execute(self, context):
        with self.open_source() as source:
            with self.open_dest() as dest:
                self.transfer(source, dest)

    def open_source(self):
        logging.info("Opening file {} on {} source {}."
            .format(self.source_path, self.source_type, self.source_conn_id))
        return self.SourceHook(self.source_conn_id).open(self.source_path, 'rb')

    def open_dest(self):
        logging.info("Opening file {} on {} dest {}."
            .format(self.dest_path, self.dest_type, self.dest_conn_id))
        return self.DestHook(self.dest_conn_id).open(self.dest_path, 'w')

    def transfer(self, source, dest):
        logging.info("Transferring data from source to destination.")
        for chunk in source:
            dest.write(chunk)

class FileTransformOperator (FileTransferOperator):
    """
    Retrieve a file from a connection, transforms it by some executable script,
    and store it on another connection.

    :param source_conn_id: The connection to run the operator against.
    :type source_conn_id: string
    :param source_path: The path to the file on the server
    :type source_path: string
    :param dest_path: The full path to which the file will be downloaded
    :type dest_path: string
    """

    template_fields = ('source_path','dest_path','transform_script')

    @apply_defaults
    def __init__(self,
                 transform_script,
                 use_stdin=True,
                 use_stdout=True,
                 *args, **kwargs):
        super(FileTransformOperator, self).__init__(*args, **kwargs)
        self.transform_script = transform_script
        self.use_stdin = use_stdin
        self.use_stdout = use_stdout

    def transfer(self, source, dest):
        # Dump the source to a temporary file
        with NamedTemporaryFile() as f_source:
            logging.info('Dumping source data to a file: ' + f_source.name)
            f_source.write(source.read())
            f_source.seek(0)

            # Open a temporary file to receive the output from the
            # transformation script
            with NamedTemporaryFile() as f_dest:
                self.transform(f_source, f_dest)
                f_dest.seek(0)

                # Write the transformed data to the destination
                logging.info('Transferring transformed data from to destination.')
                for chunk in f_dest:
                    dest.write(chunk)

    def transform(self, f_source, f_dest):
        script_args = [self.transform_script]

        if not self.use_stdin:
            script_args.append(f_source.name)

        if not self.use_stdout:
            script_args.append(f_dest.name)

        logging.info('Running the transformation script command: ' +
                     ' '.join(script_args))
        self.sp = subprocess.run(script_args,
                                 stdin=f_source if self.use_stdin else None,
                                 stdout=f_dest if self.use_stdout else None,
                                 stderr=subprocess.PIPE)

        if self.sp.returncode > 0:
            raise AirflowException("Transform script failed " + self.sp.stderr)
        else:
            logging.info("Transform script successful. "
                         "Output temporarily located at {0}"
                         "".format(f_dest.name))

class FileAvailabilitySensor (BaseSensorOperator):
    """
    Waits for a file to land in a connection.
    """
    template_fields = ('source_path',)
    ui_color = '#ffcc44'

    @apply_defaults
    def __init__(self,
                 source_type,
                 source_path,
                 source_conn_id=None,
                 dest_path=None,
                 *args, **kwargs):
        super(FileAvailabilitySensor, self).__init__(*args, **kwargs)

        self.source_type = source_type
        self.source_conn_id = source_conn_id
        self.source_path = source_path

        self.SourceHook = CommonFileHook.by_type(source_type)

    def poke(self, context):
        return self.check_source()

    def check_source(self):
        logging.info("Checking existence of file {} on {} source {}."
            .format(self.source_path, self.source_type, self.source_conn_id))
        if self.SourceHook(self.source_conn_id).file_exists(self.source_path):
            logging.info("File exist.")
            return True
        else:
            logging.info("File does not exist.")
            return False

class FolderAvailabilitySensor (FileAvailabilitySensor):
    """
    Waits for a folder to land in a connection.
    """
    def check_source(self):
        logging.info("Checking existence of folder {} on {} source {}."
            .format(self.source_path, self.source_type, self.source_conn_id))
        if self.SourceHook(self.source_conn_id).folder_exists(self.source_path):
            logging.info("Folder does not exist.")
            return True
        else:
            logging.info("Folder exists.")
            return False

class CleanupOperator(BaseOperator):
    """
    Recursively deletes a set of files or folders.
    """
    template_fields = ('paths',)
    template_ext = ()
    ui_color = '#ffcc44'

    @apply_defaults
    def __init__(self,
                 paths,
                 conn_id=None,
                 conn_type='local',
                 *args, **kwargs):
        super(CleanupOperator, self).__init__(*args, **kwargs)
        if isinstance(paths, str):
            paths = [paths]

        self.conn_type = conn_type
        self.conn_id = conn_id
        self.paths = paths

        self.ConnHook = CommonFileHook.by_type(conn_type)

    def execute(self, context):
        conn = self.ConnHook(self.conn_id)
        try:
            paths_iter = iter(self.paths)
        except TypeError:
            logging.info('Deleting path {}'.format(paths))
            conn.delete(paths)
        else:
            for path in paths_iter:
                logging.info('Deleting path {}'.format(path))
                conn.delete(path)


# =========================================================
# Plugin Definition

class FileTransferPlugin(AirflowPlugin):
    name = "file_transfer_plugin"
    operators = [CleanupOperator, FileDownloadOperator, FolderDownloadOperator,
        FileTransferOperator, FileTransformOperator,
        FileAvailabilitySensor, FolderAvailabilitySensor]
    hooks = [CommonFileHook, CommonFSHook, CommonFTPHook, CommonFTPSHook, CommonS3Hook]
