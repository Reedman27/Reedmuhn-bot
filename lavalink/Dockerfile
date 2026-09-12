# Pre-configured Lavalink node for ReedMuhn's music feature. This bakes in
# application.yml (the youtube-source plugin, filters, etc.) so `docker
# compose up` gives you a working audio backend with no manual Lavalink
# setup - override the password via the LAVALINK_SERVER_PASSWORD env var
# (see docker-compose.yml) rather than editing the baked-in file.
#
# Lavalink is a separate project (https://github.com/lavalink-devs/Lavalink,
# MIT License) - this Dockerfile just packages it with ReedMuhn's config,
# it doesn't modify Lavalink itself.
FROM eclipse-temurin:21-jre-jammy

ARG LAVALINK_VERSION=4.2.2
WORKDIR /opt/Lavalink

ADD https://github.com/lavalink-devs/Lavalink/releases/download/${LAVALINK_VERSION}/Lavalink.jar /opt/Lavalink/Lavalink.jar
COPY application.yml /opt/Lavalink/application.yml

# Matches the uid/gid the official Lavalink image uses, so a mounted
# ./plugins volume with those ownership bits (see Lavalink's docs) works
# here too without extra chown steps.
RUN groupadd -g 322 lavalink \
    && useradd -u 322 -g 322 -m -s /usr/sbin/nologin lavalink \
    && chown -R lavalink:lavalink /opt/Lavalink
USER lavalink

EXPOSE 2333
ENTRYPOINT ["java", "-jar", "Lavalink.jar"]
