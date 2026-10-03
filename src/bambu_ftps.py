"""Implicit-FTPS uploader for Bambu printers (port 990, user 'bblp')."""
import ftplib
import os
import socket
import ssl


class _ImplicitFTPS(ftplib.FTP_TLS):
    # The A1 series never answers the TLS close_notify that ftplib sends after a transfer,
    # so unwrap() hangs until it times out. With skip_unwrap we just close the data socket.
    skip_unwrap = False

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            # Reuse the control connection's TLS session (the printer requires it)
            conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                            session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        if not self.skip_unwrap:
            return super().storbinary(cmd, fp, blocksize, callback, rest)

        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
        finally:
            conn.close()  # no TLS unwrap(), see skip_unwrap above
        return self.voidresp()


def _connect(ip, access_code, timeout, skip_unwrap=False, debug=False):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ftp = _ImplicitFTPS(context=ctx)
    ftp.skip_unwrap = skip_unwrap
    if debug:
        ftp.set_debuglevel(1)
    try:
        ftp.connect(ip, 990, timeout=timeout)
        ftp.login("bblp", access_code)
        ftp.prot_p()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass
        raise
    return ftp


def upload_file(ip, access_code, local_path, remote_name, timeout=60, skip_unwrap=False, debug=False):
    """Upload a file to the printer's storage root. Returns True if the remote size matches.

    Use skip_unwrap=True for the A1 series (H2D works with the default).
    """
    try:
        ftp = _connect(ip, access_code, timeout, skip_unwrap, debug)
    except Exception as e:
        print(f"FTP upload error: {e}")
        return False

    try:
        local_size = os.path.getsize(local_path)
        try:
            with open(local_path, "rb") as f:
                ftp.storbinary(f"STOR {remote_name}", f)
        except Exception as e:
            # The printer sometimes complains as the transfer closes; the size check decides
            print(f"FTP STOR reported: {e}")

        try:
            return ftp.size(remote_name) == local_size
        except Exception as e:
            print(f"FTP SIZE failed: {e}")
            return False
    except Exception as e:
        print(f"FTP upload error: {e}")
        return False
    finally:
        try:
            ftp.close()
        except Exception:
            pass


def delete_file(ip, access_code, remote_name, timeout=30, skip_unwrap=False):
    """Delete a file from the printer's storage root. Returns True on success."""
    try:
        ftp = _connect(ip, access_code, timeout, skip_unwrap)
        try:
            ftp.delete(remote_name)
            return True
        finally:
            ftp.close()
    except Exception as e:
        print(f"FTP delete error: {e}")
        return False