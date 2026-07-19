# typed: strict
# frozen_string_literal: true

# TEMPLATE ONLY: this is not an installable or published Atlas formula.
#
# A release owner must replace the disabled state with a real immutable release
# URL, its verified SHA-256 digest, and generated Python resource blocks. Do not
# publish this file in a tap in its current form.
class AtlasDownloadManager < Formula
  include Language::Python::Virtualenv

  desc "Plan-first terminal download manager for media, files, batches, and mirrors"
  homepage "https://github.com/xkam7ar/atlas-download-manager"
  license "MIT"

  disable! date: "2026-07-16", because: "Atlas has no published package release"

  depends_on "aria2"
  depends_on "ffmpeg"
  depends_on "python@3.12"
  depends_on "wget"
  depends_on "wget2"

  conflicts_with "atlas", because: "both install an atlas executable"

  # After adding a real `url` and `sha256`, copy this template into the
  # Homebrew tap and run:
  #   brew update-python-resources atlas-download-manager
  # before publishing. Homebrew must install Python dependencies from declared
  # resource blocks, not by fetching from the network at install time.

  def install
    virtualenv_install_with_resources
  end

  test do
    system "#{bin}/atlas", "doctor", "--json"
  end
end
