"""Implicit-FTPS uploader for Bambu printers (port 990, user 'bblp')."""
import ftplib
import os
import socket
import ssl


class _ImplicitFTPS(ftplib.FTP_TLS):
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


def upload_file(ip, access_code, local_path, remote_name, timeout=60):
    """Upload a file to the printer's storage root. Returns True if the remote size matches."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ftp = _ImplicitFTPS(context=ctx)
    try:
        ftp.connect(ip, 990, timeout=timeout)
        ftp.login("bblp", access_code)
        ftp.prot_p()

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
