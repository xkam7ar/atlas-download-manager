class Atlas < Formula
  include Language::Python::Virtualenv

  desc "Intent-first download hub for media, files, batches, and mirrors"
  homepage "https://github.com/xkam7ar/atlas"
  url "https://github.com/xkam7ar/atlas/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_TARBALL_SHA256"
  license "MIT"

  depends_on "python@3.12"
  depends_on "ffmpeg"
  depends_on "aria2"
  depends_on "wget2"
  depends_on "wget"

  # Copy this template into the Homebrew tap and run:
  #   brew update-python-resources atlas
  # before publishing. Homebrew must install Python dependencies from declared
  # resource blocks, not by fetching from the network at install time.

  def install
    virtualenv_install_with_resources
  end

  test do
    system "#{bin}/atlas", "doctor", "--json"
  end
end
