FROM golang:1.27.2-alpine@sha256:3b15ba438c60ab8fabe5fee3b11025c0049e358b31feff384648f11fc8e335f8 AS builder
WORKDIR /src
COPY main.go main_test.go ./
RUN CGO_ENABLED=0 go test main.go main_test.go \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /publish main.go

FROM scratch
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=builder /publish /usr/local/bin/brain-codeql-publish
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/brain-codeql-publish"]
